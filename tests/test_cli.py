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
"""The `gffbase` console script.

Every command is exercised end to end through `main()` -- the same entry point
`[project.scripts]` installs -- with stdout captured, because the output shape
is the contract for anyone piping this into another tool.

Two rules the tests enforce throughout: **feature output goes to stdout,
progress goes to stderr**, so `gffbase rmdups x.gff > y.gff` produces a valid
file; and a command that cannot do what was asked says so in its exit status,
not only in a message.
"""

from __future__ import annotations

from pathlib import Path

import gffbase
import pytest
from gffbase import create_db
from gffbase.cli import build_parser, main

DATA = Path(__file__).parent / "data"
HIER = str(DATA / "hierarchy.gff3")


@pytest.fixture
def db_path(tmp_path):
    """An on-disk database, since most commands take a path."""
    p = tmp_path / "h.duckdb"
    create_db(HIER, str(p))
    return str(p)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def gff_lines(out: str) -> list[list[str]]:
    """Data rows only. `GFFWriter` emits a `##gff-version 3` header, which is
    part of a valid file but is not a feature."""
    return [
        line.split("\t") for line in out.splitlines() if line.strip() and not line.startswith("#")
    ]


# ---------------------------------------------------------------------------
# Parser wiring
# ---------------------------------------------------------------------------


def test_every_command_is_registered():
    """A command defined but never registered is unreachable from the shell,
    which is what happened to gffutils' `annotate` and `convert`."""
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert actions, "no subcommand action found"
    registered = set(actions[0].choices)
    assert registered == {
        "create",
        "fetch",
        "children",
        "parents",
        "region",
        "search",
        "rmdups",
        "sanitize",
        "stats",
        "validate",
        "migrate",
    }


def test_every_registered_command_has_a_handler():
    """`region` upstream is registered and raises NotImplementedError. Being
    listed in --help is not the same as working."""
    parser = build_parser()
    choices = [a for a in parser._actions if a.dest == "command"][0].choices
    for name, subparser in choices.items():
        assert subparser.get_default("func") is not None, name


def test_bare_invocation_prints_help_and_fails(capsys):
    code, out, _err = run(capsys)
    assert code == 2
    assert "usage: gffbase" in out


def test_version_flag(capsys):
    import gffbase

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert gffbase.__version__ in capsys.readouterr().out


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_create_builds_a_queryable_database(tmp_path, capsys):
    out_path = tmp_path / "made.duckdb"
    code, out, err = run(capsys, "create", HIER, "--output", str(out_path))
    assert code == 0
    assert out_path.is_file()
    assert out == "", "create must not write to stdout"
    assert "8 features" in err

    from gffbase.interface import FeatureDB

    assert FeatureDB(str(out_path))["g1"].featuretype == "gene"


def test_create_quiet_says_nothing(tmp_path, capsys):
    code, out, err = run(capsys, "create", HIER, "--output", str(tmp_path / "q.duckdb"), "--quiet")
    assert code == 0
    assert out == ""
    assert err == ""


def test_create_defaults_its_output_name(tmp_path, capsys):
    src = tmp_path / "in.gff3"
    src.write_text(Path(HIER).read_text())
    code, _out, _err = run(capsys, "create", str(src), "--quiet")
    assert code == 0
    assert (tmp_path / "in.gff3.duckdb").is_file()


def test_create_accepts_the_mode_axis(tmp_path, capsys):
    out_path = tmp_path / "strict.duckdb"
    code, _out, _err = run(
        capsys, "create", HIER, "--output", str(out_path), "--quiet", "--mode", "strict"
    )
    assert code == 0
    from gffbase.interface import FeatureDB

    assert FeatureDB(str(out_path)).mode == "strict"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


def test_fetch_prints_the_requested_features(db_path, capsys):
    """Upstream's `fetch` raises `TypeError: string indices must be integers`
    in its common case, because `get_gff_db` hands it a path string."""
    code, out, _err = run(capsys, "fetch", db_path, "g1,e1")
    assert code == 0
    rows = gff_lines(out)
    assert [r[8].split(";")[0] for r in rows] == ["ID=g1", "ID=e1"]


def test_fetch_reports_a_missing_id_in_the_exit_status(db_path, capsys):
    code, out, err = run(capsys, "fetch", db_path, "g1,nope")
    assert code == 1, "a missing id must be visible to a script, not only to a human"
    assert "nope not found" in err
    assert len(gff_lines(out)) == 1, "the ids that DID resolve are still printed"


def test_fetch_accepts_an_annotation_file_directly(capsys):
    """The `db` argument is documented as accepting a GFF file. Upstream says
    so too, and then four of its five commands ignore it."""
    code, out, _err = run(capsys, "fetch", HIER, "g1")
    assert code == 0
    assert len(gff_lines(out)) == 1


# ---------------------------------------------------------------------------
# children / parents
# ---------------------------------------------------------------------------


def test_children_walks_the_whole_hierarchy(db_path, capsys):
    code, out, _err = run(capsys, "children", db_path, "g1")
    assert code == 0
    ids = [r[8].split(";")[0] for r in gff_lines(out)]
    assert ids[0] == "ID=g1"
    assert "ID=e1" in ids and "ID=c1" in ids


def test_children_level_is_honoured(db_path, capsys):
    """Upstream hardcodes a two-level walk and raises NotImplementedError for
    any `--limit`, the argument documented to control exactly this."""
    code, out, _err = run(capsys, "children", db_path, "g1", "--level", "1")
    assert code == 0
    ids = {r[8].split(";")[0] for r in gff_lines(out)}
    assert "ID=t1" in ids
    assert "ID=e1" not in ids, "level=1 must not reach grandchildren"


def test_children_exclude_self(db_path, capsys):
    code, out, _err = run(capsys, "children", db_path, "g1", "--exclude-self")
    assert "ID=g1" not in {r[8].split(";")[0] for r in gff_lines(out)}


def test_children_exclude_featuretypes(db_path, capsys):
    code, out, _err = run(capsys, "children", db_path, "g1", "--exclude", "exon,CDS")
    assert code == 0
    assert {r[2] for r in gff_lines(out)} == {"gene", "mRNA"}


def test_children_defaults_to_every_gene(db_path, capsys):
    code, out, _err = run(capsys, "children", db_path)
    assert code == 0
    assert "ID=g1" in {r[8].split(";")[0] for r in gff_lines(out)}


def test_parents_walks_upward(db_path, capsys):
    code, out, _err = run(capsys, "parents", db_path, "e1")
    assert code == 0
    ids = {r[8].split(";")[0] for r in gff_lines(out)}
    assert ids >= {"ID=e1", "ID=t1", "ID=g1"}


# ---------------------------------------------------------------------------
# region / search -- both stubs or broken upstream
# ---------------------------------------------------------------------------


def test_region_returns_overlapping_features(db_path, capsys):
    """Registered upstream, and raises `NotImplementedError` on every call."""
    code, out, _err = run(capsys, "region", db_path, "chr1:1-10000")
    assert code == 0
    assert len(gff_lines(out)) > 0


def test_region_restricts_by_featuretype(db_path, capsys):
    code, out, _err = run(capsys, "region", db_path, "chr1:1-10000", "--featuretype", "exon")
    assert code == 0
    assert {r[2] for r in gff_lines(out)} == {"exon"}


def test_region_completely_within(db_path, capsys):
    _c, wide, _e = run(capsys, "region", db_path, "chr1:150-250")
    _c, narrow, _e = run(capsys, "region", db_path, "chr1:150-250", "--completely-within")
    assert len(gff_lines(narrow)) < len(gff_lines(wide))


def test_search_finds_features_by_attribute_value(db_path, capsys):
    """Upstream calls `db.attribute_search`, which exists nowhere in gffutils,
    so this command raises `AttributeError` on every invocation."""
    code, out, _err = run(capsys, "search", db_path, "geneA")
    assert code == 0
    assert [r[8].split(";")[0] for r in gff_lines(out)] == ["ID=g1"]


def test_search_is_case_insensitive(db_path, capsys):
    _c, lower, _e = run(capsys, "search", db_path, "genea")
    _c, upper, _e = run(capsys, "search", db_path, "GENEA")
    assert lower == upper != ""


def test_search_restricts_by_featuretype(db_path, capsys):
    code, out, _err = run(capsys, "search", db_path, "g1", "--featuretype", "mRNA")
    assert code == 0
    assert {r[2] for r in gff_lines(out)} == {"mRNA"}


# ---------------------------------------------------------------------------
# rmdups / sanitize
# ---------------------------------------------------------------------------


def test_rmdups_writes_only_gff_to_stdout(tmp_path, capsys):
    """Upstream prints its banner to stdout, landing in the middle of the GFF
    it is writing -- so `gffutils-cli rmdups x.gff > y.gff` is corrupt."""
    src = tmp_path / "dup.gff3"
    src.write_text(
        "chr1\ts\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\ts\tgene\t1\t100\t.\t+\t.\tID=g1\n"
        "chr1\ts\tgene\t200\t300\t.\t+\t.\tID=g2\n"
    )
    code, out, err = run(capsys, "rmdups", str(src))
    assert code == 0
    assert "Removing duplicates" in err
    for line in out.splitlines():
        if line.strip():
            assert line.startswith("#") or len(line.split("\t")) >= 8, repr(line)
    ids = [r[8].split(";")[0] for r in gff_lines(out)]
    assert sorted(ids) == ["ID=g1", "ID=g2"]


def test_sanitize_orders_coordinates(tmp_path, capsys):
    src = tmp_path / "bad.gff3"
    src.write_text(
        "chr1\ts\tgene\t100\t200\t.\t+\t.\tID=g1\n"
        "chr1\ts\tmRNA\t100\t200\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\ts\texon\t180\t150\t.\t+\t.\tID=e1;Parent=t1\n"
    )
    code, out, err = run(capsys, "sanitize", str(src))
    assert code == 0
    assert "Sanitizing GFF" in err
    for row in gff_lines(out):
        assert int(row[3]) <= int(row[4]), row
        assert "gid=" in row[8]


# ---------------------------------------------------------------------------
# validate / migrate -- gffbase only
# ---------------------------------------------------------------------------


def test_validate_reports_every_invariant(db_path, capsys):
    code, out, err = run(capsys, "validate", db_path)
    assert code == 0
    assert out.count("ok      INV-") >= 12
    assert "0 error(s), 0 warning(s)" in err


def _corrupt(db_path: str) -> None:
    """Delete a feature but leave its edges behind, so an edge endpoint no
    longer resolves (INV-6). Duplicating an id is not an option: `features.id`
    is a primary key, so the database refuses that corruption itself."""
    from gffbase.interface import FeatureDB

    db = FeatureDB(db_path)
    db.conn.execute("DELETE FROM features WHERE id = 'e1'")
    db.conn.close()


def test_validate_reports_a_corrupt_database(db_path, capsys):
    """Dangling edges and attributes are errors, not a successful warning."""
    _corrupt(db_path)
    code, _out, err = run(capsys, "validate", db_path)
    assert "INV-6" in err
    assert "edges_resolve" in err
    assert "INV-15" in err
    assert "no_orphan_attributes" in err
    assert "1 error(s), 1 warning(s)" in err
    assert code == 1


def test_validate_strict_fails_on_warnings(db_path, capsys):
    """`--strict` is the CI form: any violation at all is a failure."""
    _corrupt(db_path)
    code, _out, err = run(capsys, "validate", db_path, "--strict")
    assert code == 1
    assert "INV-6" in err


def test_validate_strict_is_still_green_on_a_healthy_database(db_path, capsys):
    """`--strict` must not fail everything -- otherwise it is useless."""
    code, _out, err = run(capsys, "validate", db_path, "--strict")
    assert code == 0
    assert "0 error(s), 0 warning(s)" in err


def test_migrate_is_a_noop_on_a_current_database(db_path, capsys):
    code, _out, err = run(capsys, "migrate", db_path)
    assert code == 0
    assert "schema:" in err


# ---------------------------------------------------------------------------
# CLI parity against the recorded oracle surface
# ---------------------------------------------------------------------------
#
# `tools/gen_parity_manifest.py --check` diffs only the `modules` block, so the
# `cli` block it records is inert data and CLI drift is invisible to it. These
# close that gap.


def _oracle_cli() -> dict:
    import json

    manifest = json.loads((Path(__file__).parent / "parity" / "gffutils_manifest.json").read_text())
    return manifest["cli"]


def test_the_manifest_still_records_the_oracle_cli():
    cli = _oracle_cli()
    assert cli["found"] is True
    assert len(cli["commands"]) == 13


def test_every_working_upstream_command_is_reproduced():
    """A command that runs upstream must run here.

    Stubs are excluded by name and each one is asserted to still BE a stub, so
    that if upstream ever implements one this test says so rather than silently
    letting the gap persist.
    """
    cli = _oracle_cli()
    stubs = {"annotate", "clean", "common", "convert", "region", "handle_relations_args"}
    for name in stubs:
        entry = cli["commands"].get(name)
        assert entry is not None, f"{name} vanished from the manifest"
        assert entry["is_stub"], f"{name} is no longer a stub upstream; reconsider excluding it"

    # `handle_relations_args` is a shared helper, not a subcommand -- its
    # `is_stub` flag comes from a conditional `raise` inside it.
    real = {
        name
        for name, entry in cli["commands"].items()
        if not entry["is_stub"] and name != "handle_relations_args"
    }
    assert real == {"children", "create", "fetch", "parents", "rmdups", "sanitize", "search"}

    ours = set([a for a in build_parser()._actions if a.dest == "command"][0].choices)
    missing = real - ours
    assert not missing, f"upstream commands with no gffbase equivalent: {sorted(missing)}"


def test_gffbase_implements_the_upstream_stubs_too():
    """`region` is registered upstream and raises; `search` and `fetch` are
    marked working and raise anyway. All three work here."""
    ours = set([a for a in build_parser()._actions if a.dest == "command"][0].choices)
    assert {"region", "search", "fetch"} <= ours


# ---------------------------------------------------------------------------
# Stream ownership
# ---------------------------------------------------------------------------


def test_gffwriter_does_not_close_a_stream_it_was_given():
    """`GFFWriter(sys.stdout).close()` used to shut stdout down for the whole
    process, so anything written afterwards raised `ValueError: I/O operation
    on closed file`. A writer owns only the handles it opened."""
    import io

    from gffbase.gffwriter import GFFWriter

    buf = io.StringIO()
    writer = GFFWriter(buf)
    writer.close()
    assert not buf.closed
    buf.write("still usable")


def test_gffwriter_does_close_a_file_it_opened(tmp_path):
    """The other half: a path it opened must be closed, or the content may
    never reach disk."""
    from gffbase.gffwriter import GFFWriter

    target = tmp_path / "out.gff3"
    writer = GFFWriter(str(target))
    writer.write_rec("chr1\ts\tgene\t1\t9\t.\t+\t.\tID=g1")
    writer.close()
    assert writer._fh.closed
    assert "ID=g1" in target.read_text()


def test_migrate_coalesce_refuses_a_current_database(db_path, capsys):
    """`--coalesce` is the separate, opt-in second step: the migration proper
    is structural and changes no query result, while coalescing re-fuses v1's
    split multipart rows and therefore DOES change results. On an already-v2
    database there is nothing to fuse."""
    code, _out, err = run(capsys, "migrate", db_path, "--coalesce")
    assert code == 0
    assert "coalesced" in err


# ---------------------------------------------------------------------------
# `python -m gffbase`
# ---------------------------------------------------------------------------


def test_python_dash_m_runs_the_cli():
    """`python -m gffbase --version` must work, not just the console script.

    `__main__.py` was covered only by a CI smoke step, so it read as 0% in
    every coverage report and a breakage would have surfaced on a runner
    rather than here. It is three lines and a subprocess away from being
    tested properly.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "gffbase", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert gffbase.__version__ in (proc.stdout + proc.stderr)


def test_python_dash_m_reports_usage_for_no_arguments():
    """Bare `python -m gffbase` exits non-zero and says how to use it."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "gffbase"], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode != 0
    assert "usage" in (proc.stdout + proc.stderr).lower()


# ---------------------------------------------------------------------------
# `gffbase stats`
# ---------------------------------------------------------------------------


def test_stats_reports_the_shape_of_the_database(tmp_path, capsys):
    """The first question anyone asks of an unfamiliar annotation."""
    src = tmp_path / "s.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tsrc\tgene\t100\t900\t.\t+\t.\tID=g1\n"
        "chr1\tsrc\tmRNA\t100\t900\t.\t+\t.\tID=t1;Parent=g1\n"
        "chr1\tsrc\texon\t100\t200\t.\t+\t.\tID=e1;Parent=t1\n"
        "chr2\tsrc\texon\t100\t200\t.\t+\t.\tID=e2;Parent=t1\n"
    )
    out = str(tmp_path / "s.duckdb")
    create_db(str(src), out, force=True).close()

    assert main(["stats", out]) == 0
    captured = capsys.readouterr().out

    assert "features    4" in captured
    # Per-featuretype breakdown, biggest first.
    assert "exon" in captured and "gene" in captured
    # Both sequences, and the count.
    assert "sequences   2" in captured
    assert "chr1" in captured and "chr2" in captured
    # Provenance the database records about itself.
    assert "gff3" in captured
    assert "compat" in captured


def test_stats_reports_discontinuous_features(tmp_path, capsys):
    """A multipart corpus says so; an ordinary one stays quiet about it."""
    src = tmp_path / "m.gff3"
    src.write_text(
        "##gff-version 3\n"
        "chr1\tsrc\tgene\t100\t900\t.\t+\t.\tID=g1\n"
        "chr1\tsrc\tCDS\t100\t200\t.\t+\t0\tID=c1;Parent=g1\n"
        "chr1\tsrc\tCDS\t500\t600\t.\t+\t2\tID=c1;Parent=g1\n"
    )
    out = str(tmp_path / "m.duckdb")
    create_db(str(src), out, force=True, mode="strict").close()

    assert main(["stats", out]) == 0
    assert "discontinuous features" in capsys.readouterr().out
