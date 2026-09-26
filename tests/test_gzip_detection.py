"""gzip input is recognized by its magic bytes, not by a `.gz` suffix.

0.2.0 decided by extension in both engines. A `.bgz` file (bgzip's usual
name), a `.GZ` file, or a gzip file with no extension was read as text; the
compressed bytes parsed as zero features and ingest built an empty database
without raising.
"""

from __future__ import annotations

import gzip

import pytest
from gffbase import DataIterator, create_db, native_available
from gffbase.ingest import from_file

ENGINES = ["python"] + (["rust"] if native_available() else [])

TEXT = (
    "##gff-version 3\n"
    "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1\n"
    "chr1\tt\tmRNA\t1\t100\t.\t+\t.\tID=t1;Parent=g1\n"
    "chr1\tt\texon\t1\t50\t.\t+\t.\tID=e1;Parent=t1\n"
)
IDS = ["g1", "t1", "e1"]

GZIPPED_NAMES = ["a.gff3.gz", "a.gff3.bgz", "a.GFF3.GZ", "a.gz.gff3", "no_extension"]


def _ids(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT id FROM features ORDER BY file_order").fetchall()]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("name", GZIPPED_NAMES)
def test_gzip_content_is_decompressed_whatever_the_name(tmp_path, engine, name):
    src = tmp_path / name
    src.write_bytes(gzip.compress(TEXT.encode()))
    con, _ = from_file(str(src), ":memory:", engine=engine)
    assert _ids(con) == IDS


@pytest.mark.parametrize("engine", ENGINES)
def test_every_member_of_a_multi_member_stream_is_read(tmp_path, engine):
    """bgzip output is a series of independent gzip members."""
    lines = TEXT.splitlines(keepends=True)
    src = tmp_path / "multi.gff3.bgz"
    src.write_bytes(b"".join(gzip.compress(line.encode()) for line in lines))
    con, _ = from_file(str(src), ":memory:", engine=engine)
    assert _ids(con) == IDS


@pytest.mark.parametrize("engine", ENGINES)
def test_plain_text_named_gz_is_read_as_text(tmp_path, engine):
    src = tmp_path / "mislabelled.gff3.gz"
    src.write_text(TEXT)
    con, _ = from_file(str(src), ":memory:", engine=engine)
    assert _ids(con) == IDS


@pytest.mark.parametrize("name", GZIPPED_NAMES)
def test_create_db_and_data_iterator_agree(tmp_path, name):
    src = tmp_path / name
    src.write_bytes(gzip.compress(TEXT.encode()))
    db = create_db(str(src), ":memory:")
    assert [f.id for f in db.all_features()] == IDS
    assert [f.attributes["ID"][0] for f in DataIterator(str(src))] == IDS


@pytest.mark.parametrize("engine", ENGINES)
def test_a_gtf_in_a_bgz_file_gets_its_hierarchy(tmp_path, engine):
    gtf = (
        'chr1\tt\texon\t1\t50\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'
        'chr1\tt\texon\t80\t100\t.\t+\t.\tgene_id "G"; transcript_id "T";\n'
    )
    src = tmp_path / "x.gtf.bgz"
    src.write_bytes(gzip.compress(gtf.encode()))
    con, _ = from_file(str(src), ":memory:", engine=engine)
    types = con.execute(
        "SELECT featuretype, count(*) FROM features GROUP BY 1 ORDER BY 1"
    ).fetchall()
    assert types == [("exon", 2), ("gene", 1), ("transcript", 1)]
