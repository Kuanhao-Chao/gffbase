"""GitHub issue #1: `parse_gff(strict=True)` accepted three malformed rows.

Reported against 0.1.0 by the DuckHTS authors: a tenth tab-separated field,
an attribute segment with no `=` (`ID=ok;broken`), and an unknown start with
a concrete `end=0`. Fixed in 0.2.0; these pin the fix in both engines, in
`parse_gff` and in `create_db(mode="strict")`, and pin that compat mode --
which exists to read what gffutils reads -- still loads each row while
recording what was wrong with it.
"""

from __future__ import annotations

import pytest
from gffbase import GFFFormatError, create_db, native_available
from gffbase.parser import parse_gff

ENGINES = ["python"] + (["rust"] if native_available() else [])

ROWS = {
    "extra_field": (
        b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=ok\textra\n",
        "TooManyFields",
        "ok",
    ),
    "partial_malformed_attribute": (
        b"chr1\tsrc\texon\t1\t10\t.\t+\t.\tID=ok;broken\n",
        "InvalidAttribute",
        "ok",
    ),
    "nonpositive_end_with_unknown_start": (
        b"chr1\tsrc\tregion\t.\t0\t.\t.\t.\tID=x\n",
        "InvalidCoordinate",
        "x",
    ),
}


def _write(tmp_path, name: str) -> str:
    path = tmp_path / f"{name}.gff3"
    path.write_bytes(b"##gff-version 3\n" + ROWS[name][0])
    return str(path)


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", sorted(ROWS))
def test_strict_parsing_rejects_the_row(tmp_path, engine, name):
    with pytest.raises(GFFFormatError) as info:
        list(parse_gff(_write(tmp_path, name), strict=True, engine=engine))
    assert info.value.line_no == 2
    assert info.value.kind == ROWS[name][1]


@pytest.mark.parametrize("name", sorted(ROWS))
def test_a_strict_database_rejects_the_row(tmp_path, name):
    with pytest.raises(GFFFormatError, match="line 2"):
        create_db(_write(tmp_path, name), ":memory:", mode="strict")


@pytest.mark.parametrize("name", sorted(ROWS))
def test_compat_loads_the_row_and_says_what_was_wrong(tmp_path, name):
    db = create_db(_write(tmp_path, name), ":memory:")
    assert [f.id for f in db.all_features()] == [ROWS[name][2]]
    assert ROWS[name][1] in {w["kind"] for w in db.warnings}
