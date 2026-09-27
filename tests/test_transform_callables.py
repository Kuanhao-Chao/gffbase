"""`transform` and callable `id_spec` see and change features as in gffutils.

Through 0.2.1 the object handed to these callables was a thin view that
lacked `f.id`, `len(f)`, `str(f)`, `f.dialect` and `f[key] = value`, and --
worse -- a transform's edits to `f.attributes` were dropped: renaming a gene
or adding a key stored the original and raised nothing.
"""

from __future__ import annotations

import pytest
from gffbase import create_db

GFF = (
    "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Name=a\n"
    "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1\n"
)
GTF = (
    'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
    'chr1\tt\texon\t80\t100\t.\t+\t.\tgene_id "G1"; transcript_id "T1";\n'
)


def rename(f):
    f.attributes["Name"] = ["renamed"]
    f.attributes["new"] = "x"  # a bare value is wrapped, as in gffutils
    return f


def version_ids(f):
    f.attributes["ID"] = [f.attributes["ID"][0] + "_v2"]
    return f


def set_item(f):
    f["tag"] = "t"
    return f


def fields(f):
    f.featuretype = f.featuretype.upper()
    f.start = f.start + 1
    f.chrom = "chrX"
    return f


def peek(f):
    # Everything a gffutils transform commonly reads, before ids exist.
    ok = f.id is None and f.chrom == "chr1" and f.stop == 100 and len(f) == 100
    return f if ok and str(f).startswith("chr1\tt\t") and f.dialect["fmt"] == "gff3" else None


def snapshot(db):
    rows = sorted(
        (f.id, f.seqid, f.featuretype, f.start, {k: list(v) for k, v in f.attributes.items()})
        for f in db.all_features()
    )
    rels = sorted((p.id, c.id) for c in db.all_features() for p in db.parents(c, level=1))
    return rows, rels


TRANSFORMS = {
    "rename": rename,
    "version_ids": version_ids,
    "set_item": set_item,
    "fields": fields,
    "peek": peek,
}


def test_attribute_edits_are_stored():
    db = create_db(GFF, ":memory:", from_string=True, transform=rename)
    assert db["g1"].attributes["Name"] == ["renamed"]
    assert db["g1"].attributes["new"] == ["x"]
    assert "Name=renamed" in str(db["g1"])
    report = db.validate(level="full")
    assert report.ok, report.errors


def test_an_edited_id_attribute_names_the_feature():
    db = create_db(GFF, ":memory:", from_string=True, transform=version_ids)
    assert sorted(f.id for f in db.all_features()) == ["g1_v2", "t1_v2"]


def test_untouched_attributes_stay_byte_faithful():
    db = create_db(GFF, ":memory:", from_string=True, transform=fields)
    assert str(db["g1"]).endswith("\tID=g1;Name=a")


def test_a_featureless_length_is_not_a_drop():
    text = "chr1\tt\tgene\t.\t.\t.\t+\t.\tID=nocoord\n"
    db = create_db(text, ":memory:", from_string=True, transform=lambda f: f)
    assert "nocoord" in db


def test_gtf_edits_render_in_gtf():
    def tag(f):
        f.attributes["gene_name"] = ["BRCA1"]
        return f

    db = create_db(GTF, ":memory:", from_string=True, transform=tag)
    exon = next(db.features_of_type("exon"))
    assert exon.attributes["gene_name"] == ["BRCA1"]
    assert 'gene_name "BRCA1"' in str(exon)


def test_an_id_spec_callable_sees_the_same_view():
    seen = []

    def spec(f):
        seen.append((f.id, len(f), f.chrom))
        return "X_" + f.attributes["ID"][0]

    db = create_db(GFF, ":memory:", from_string=True, id_spec=spec)
    assert sorted(f.id for f in db.all_features()) == ["X_g1", "X_t1"]
    assert seen[0] == (None, 100, "chr1")


@pytest.mark.parity
@pytest.mark.parametrize("name", sorted(TRANSFORMS))
def test_matches_gffutils(name):
    gffutils = pytest.importorskip("gffutils")
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        oracle = gffutils.create_db(GFF, ":memory:", from_string=True, transform=TRANSFORMS[name])
    ours = create_db(GFF, ":memory:", from_string=True, transform=TRANSFORMS[name])
    assert snapshot(ours) == snapshot(oracle)


def test_a_transform_may_return_a_gffbase_feature():
    """What a gffutils transform often does: build a fresh Feature."""
    from gffbase import Feature

    def rebuild(f):
        return Feature(
            seqid=f.seqid,
            source="rebuilt",
            featuretype=f.featuretype,
            start=f.start,
            end=f.end,
            strand=f.strand,
            # `f.id` is None here, as in gffutils: ids are assigned after.
            attributes={"ID": [f.attributes["ID"][0] + "_new"], "Note": "n"},
        )

    db = create_db(GFF, ":memory:", from_string=True, transform=rebuild)
    assert sorted(f.id for f in db.all_features()) == ["g1_new", "t1_new"]
    g = db["g1_new"]
    assert (g.source, dict(g.attributes)) == ("rebuilt", {"ID": ["g1_new"], "Note": ["n"]})


def test_str_of_the_view_is_the_line_as_edited():
    lines = []

    def record(f):
        lines.append(str(f))
        f.attributes["Name"] = ["b"]
        lines.append(str(f))
        return f

    create_db(GFF.splitlines()[0] + "\n", ":memory:", from_string=True, transform=record)
    assert lines[0] == "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Name=a"
    assert lines[1] == "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Name=b"
