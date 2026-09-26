"""A query issued inside a streaming loop must not end that loop early.

A DuckDB connection holds one result set. Every feature iterator used to stream
on the shared `FeatureDB.conn`, so any query run while an iterator was
suspended -- `db.children(gene)` inside `for gene in db.features_of_type(...)`,
the canonical gffutils idiom -- discarded the rows the outer iterator had not
fetched yet. The outer loop then stopped at its first chunk boundary (10,000
rows), silently. On GENCODE that is ~84% of genes gone with no error.

`create_introns`, `create_splice_sites` and `iter_by_parent_childs` nest the
same way internally, so they were truncated too.

These tests shrink the fetch chunk to one row so every row is a boundary;
`test_nested_loop_over_real_chunk_size` covers the default.
"""

from __future__ import annotations

import duckdb
import gffbase
import pytest
from gffbase import FeatureDB
from gffbase.exceptions import ClosedDatabaseError

N_GENES = 30


def _annotation(n_genes: int) -> str:
    lines = ["##gff-version 3"]
    for i in range(n_genes):
        s = 1 + i * 1000
        lines.append(f"chr1\tt\tgene\t{s}\t{s + 500}\t.\t+\t.\tID=g{i}")
        lines.append(f"chr1\tt\tmRNA\t{s}\t{s + 500}\t.\t+\t.\tID=t{i};Parent=g{i}")
        lines.append(f"chr1\tt\texon\t{s}\t{s + 100}\t.\t+\t.\tID=e{i}a;Parent=t{i}")
        lines.append(f"chr1\tt\texon\t{s + 300}\t{s + 500}\t.\t+\t.\tID=e{i}b;Parent=t{i}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(FeatureDB, "_CHUNK", 1)
    path = tmp_path / "nested.gff3"
    path.write_text(_annotation(N_GENES))
    handle = gffbase.create_db(str(path), str(tmp_path / "nested.duckdb"))
    yield handle
    handle.close()


# Every way a caller can query the database from inside a loop.
INNER = {
    "getitem": lambda db, f: db[f.id],
    "contains": lambda db, f: f.id in db,
    "children": lambda db, f: list(db.children(f, level=1)),
    "parents": lambda db, f: list(db.parents(f, level=1)),
    "region": lambda db, f: list(db.region(seqid=f.seqid, start=f.start, end=f.end)),
    "features_of_type": lambda db, f: sum(1 for _ in db.features_of_type("mRNA")),
    "count": lambda db, f: db.count_features_of_type("exon"),
    "execute": lambda db, f: db.execute("SELECT count(*) FROM features").fetchone(),
    "partial_inner_stream": lambda db, f: next(db.all_features(), None),
}

# Every streaming iterator a caller can loop over, and how many rows it yields.
OUTER = {
    "all_features": (lambda db: db.all_features(), N_GENES * 4),
    "features_of_type": (lambda db: db.features_of_type("gene"), N_GENES),
    "region": (lambda db: db.region(seqid="chr1"), N_GENES * 4),
    "features_of_type_exon": (lambda db: db.features_of_type("exon"), N_GENES * 2),
    "children": (lambda db: db.children("g0"), 3),
    "parents": (lambda db: db.parents("e0a"), 2),
    "attribute_search": (lambda db: db.attribute_search("g"), None),
}


@pytest.mark.parametrize("inner", sorted(INNER))
@pytest.mark.parametrize("outer", sorted(OUTER))
def test_a_query_inside_a_loop_does_not_end_the_loop(db, outer, inner):
    make, expected = OUTER[outer]
    if expected is None:
        expected = sum(1 for _ in make(db))
    visited = 0
    for feature in make(db):
        visited += 1
        INNER[inner](db, feature)
    assert visited == expected


def test_the_gffutils_idiom_visits_every_gene_and_child(db):
    genes = children = 0
    for gene in db.features_of_type("gene"):
        genes += 1
        for _ in db.children(gene, level=1):
            children += 1
    assert (genes, children) == (N_GENES, N_GENES)


def test_two_interleaved_streams_both_run_to_the_end(db):
    """Two iterators advanced alternately, neither exhausted before the other."""
    a, b = db.features_of_type("gene"), db.features_of_type("exon")
    got_a, got_b = [], []
    for _ in range(N_GENES * 2):
        if (x := next(a, None)) is not None:
            got_a.append(x.id)
        if (y := next(b, None)) is not None:
            got_b.append(y.id)
    assert len(got_a) == N_GENES and len(got_b) == N_GENES * 2


def test_interfeatures_over_a_live_stream_sees_every_feature(db):
    """`interfeatures` and `merge` consume the iterator they are given while
    the caller keeps querying; the gaps must span the whole stream."""
    gaps = []
    for gap in db.interfeatures(db.features_of_type("gene")):
        list(db.children("g0", level=1))
        gaps.append(gap)
    assert len(gaps) == N_GENES - 1


def test_merge_over_a_live_stream_sees_every_feature(db):
    merged = []
    for m in db.merge(db.features_of_type("exon")):
        db.count_features_of_type("gene")
        merged.append(m)
    # the exons do not overlap, so nothing collapses
    assert len(merged) == N_GENES * 2


def test_create_introns_covers_every_transcript(db):
    introns = list(db.create_introns())
    assert len(introns) == N_GENES


def test_create_splice_sites_covers_every_transcript(db):
    sites = list(db.create_splice_sites())
    # one donor and one acceptor per intron
    assert len(sites) == 2 * N_GENES


def test_iter_by_parent_childs_yields_every_parent(db):
    groups = list(db.iter_by_parent_childs("gene"))
    assert len(groups) == N_GENES
    assert all(len(g) == 4 for g in groups)  # gene, mRNA, two exons


def test_close_releases_suspended_streams(db):
    """A half-consumed iterator must not keep a connection alive past close(),
    and resuming it must say why it cannot continue."""
    stream = db.all_features()
    next(stream)
    db.close()
    with pytest.raises(ClosedDatabaseError):
        next(stream)


def test_abandoned_streams_do_not_accumulate_cursors(db):
    for _ in range(50):
        next(db.all_features())  # abandoned after one row
    assert len(db._stream_cursors) <= 1
    assert len(db._idle_cursors) <= FeatureDB._MAX_IDLE_CURSORS


def test_a_reused_cursor_starts_its_new_query_cleanly(db):
    """A stream abandoned mid-result hands its cursor back; the next stream
    to take it must see its own rows, not the leftovers."""
    stream = db.features_of_type("exon")
    next(stream)
    stream.close()
    assert db._idle_cursors
    assert [f.id for f in db.features_of_type("gene")] == [f"g{i}" for i in range(N_GENES)]


def test_close_closes_idle_cursors(db):
    list(db.all_features())
    idle = list(db._idle_cursors)
    assert idle
    db.close()
    assert not db._idle_cursors
    for cur in idle:
        with pytest.raises(duckdb.Error):
            cur.execute("SELECT 1")


@pytest.mark.slow
def test_nested_loop_over_real_chunk_size(tmp_path, monkeypatch):
    """The unshrunk default: 25,000 genes spans two chunk boundaries."""
    n = 25_000
    lines = ["##gff-version 3"]
    for i in range(n):
        s = 1 + i * 100
        lines.append(f"chr1\tt\tgene\t{s}\t{s + 50}\t.\t+\t.\tID=g{i}")
        lines.append(f"chr1\tt\tmRNA\t{s}\t{s + 50}\t.\t+\t.\tID=t{i};Parent=g{i}")
    path = tmp_path / "big.gff3"
    path.write_text("\n".join(lines) + "\n")
    with gffbase.create_db(str(path), str(tmp_path / "big.duckdb")) as db:
        genes = children = 0
        for gene in db.features_of_type("gene"):
            genes += 1
            children += sum(1 for _ in db.children(gene, level=1))
        assert (genes, children) == (n, n)
