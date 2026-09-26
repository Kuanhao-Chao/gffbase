"""A GFF3 value wholly in quotes reads the same everywhere.

gffutils reads `ID="g001"` as the value `g001` in quotes: the quotes go, and
come back on output because the dialect records quoted values. gffbase's
compat ingest already stripped them -- the stored id was `g001` and `Parent`
edges resolved -- but `Feature.attributes` re-parsed the raw column 9 without
the rule, so in 0.2.0:

* `db["g001"].attributes["ID"]` was `['"g001"']`, disagreeing with the
  feature's own id and with the `attributes` table;
* `str(feature)` after reading attributes doubled the quotes
  (`ID=""g001""`), corrupting anything written back out;
* the database failed its own full validation (INV-12).

`feature_from_line` kept the quotes too, where gffutils strips them.
"""

from __future__ import annotations

import pytest
from gffbase import create_db
from gffbase.feature import feature_from_line
from gffbase.validate import validate_db

QUOTED = (
    "##gff-version 3\n"
    'chr1\tt\tgene\t1\t100\t.\t+\t.\tID="g001";Name="BRCA1"\n'
    'chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID="t001";Parent="g001"\n'
    'chr1\tt\texon\t1\t50\t.\t+\t.\tID=e001;Parent=t001;Note=""\n'
)


@pytest.fixture
def db():
    return create_db(QUOTED, ":memory:", from_string=True)


def test_attributes_agree_with_the_stored_id(db):
    gene = db["g001"]
    assert gene.id == "g001"
    assert gene.attributes["ID"] == ["g001"]
    assert gene.attributes["Name"] == ["BRCA1"]
    assert db["t001"].attributes["Parent"] == ["g001"]


def test_quoted_parents_resolve(db):
    assert [f.id for f in db.children("g001", level=None)] == ["t001", "e001"]


def test_output_is_byte_faithful_until_attributes_are_read(db):
    gene = db["g001"]
    assert str(gene).endswith('ID="g001";Name="BRCA1"')


def test_output_after_reading_attributes_does_not_double_the_quotes(db):
    gene = db["g001"]
    gene.attributes["Name"]
    col9 = str(gene).split("\t")[8]
    assert '""' not in col9
    assert col9 == 'ID="g001";Name="BRCA1"'


def test_an_edited_feature_round_trips_through_its_own_output(db):
    gene = db["g001"]
    gene.attributes["Note"] = ["edited"]
    again = feature_from_line(str(gene))
    assert dict(again.attributes) == {"ID": ["g001"], "Name": ["BRCA1"], "Note": ["edited"]}


def test_the_compat_database_passes_full_validation(db):
    report = validate_db(db, level="full", sample=None)
    assert report.ok, report.errors


def test_feature_from_line_strips_whole_value_quotes():
    f = feature_from_line('chr1\tt\tgene\t1\t100\t.\t+\t.\tID="g001";Note=plain')
    assert dict(f.attributes) == {"ID": ["g001"], "Note": ["plain"]}


@pytest.mark.parametrize(
    ("col9", "expected"),
    [
        ('ID=g2;Note="a,b",c', {"ID": ["g2"], "Note": ['"a,b"', "c"]}),  # not the whole value
        ('ID=g3;Note=5"UTR', {"ID": ["g3"], "Note": ['5"UTR']}),  # an inner quote is data
        ('ID=g4;Note="abc', {"ID": ["g4"], "Note": ['"abc']}),  # unbalanced
    ],
)
def test_only_a_whole_quoted_value_loses_its_quotes(col9, expected):
    db = create_db(f"chr1\tt\tgene\t1\t100\t.\t+\t.\t{col9}\n", ":memory:", from_string=True)
    (feature,) = db.all_features()
    assert dict(feature.attributes) == expected
    assert validate_db(db, level="full", sample=None).ok


@pytest.mark.parity
def test_attributes_match_gffutils():
    gffutils = pytest.importorskip("gffutils")
    import warnings

    text = 'chr1\tt\tgene\t1\t100\t.\t+\t.\tID="g001";Name="BRCA1"\n'
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oracle = gffutils.create_db(text, ":memory:", from_string=True)
    ours = create_db(text, ":memory:", from_string=True)
    assert dict(ours["g001"].attributes) == dict(oracle["g001"].attributes)
    line = text.rstrip("\n")
    oracle_line = gffutils.feature.feature_from_line(line)
    assert dict(feature_from_line(line).attributes) == dict(oracle_line.attributes)
    assert str(feature_from_line(line)) == str(oracle_line)
