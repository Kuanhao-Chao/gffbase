"""The Rust ingest producer builds exactly the database the Python loop did.

`ingest._native_producer` moves the per-row half of ingest -- id resolution,
generated ids, the duplicate strategies, batching -- out of Python. The
Python loop stays (for `transform=`, callable id specs and the pure-Python
engine) and is the oracle here: every fixture, under every option that
changes a row, must dump the same tables either way, row order included
where it is observable.
"""

from __future__ import annotations

import os

import pytest
from gffbase import ingest, native_available
from gffbase._options import IngestOptions

pytestmark = pytest.mark.skipif(not native_available(), reason="needs the native engine")

DATA = os.environ.get("GFFBASE_GFFUTILS_DATA", "/ccb/salz3/kh.chao/gffutils/gffutils/test/data")
FIXTURES = sorted(
    f
    for f in (os.listdir(DATA) if os.path.isdir(DATA) else [])
    if not f.endswith((".db", ".sqlite", ".fa", ".fasta", ".bed", ".py", ".json", ".csv"))
)

OPTIONS = {
    "default": {},
    "create_unique": {"merge_strategy": "create_unique"},
    "warning": {"merge_strategy": "warning"},
    "merge": {"merge_strategy": "merge"},
    "replace": {"merge_strategy": "replace"},
    "strict": {"mode": "strict", "on_multipart_conflict": "split"},
    "id_spec=Name": {"id_spec": "Name", "merge_strategy": "create_unique"},
    "id_spec=[Name,ID]": {"id_spec": ["Name", "ID"], "merge_strategy": "create_unique"},
    "id_spec=dict": {"id_spec": {"gene": "Name"}, "merge_strategy": "create_unique"},
    "id_spec=column": {"id_spec": [":seqid:", "ID"], "merge_strategy": "create_unique"},
    "checklines=0": {"checklines": 0, "merge_strategy": "create_unique"},
    "force_gff": {"force_gff": True, "merge_strategy": "create_unique"},
}

SORTED = (
    "edges",
    "closure",
    "segments",
    "seqid_map",
    "directives",
    "autoincrements",
    "duplicates",
    "id_conflicts",
    "meta",
)


def _dump(con) -> dict:
    out = {}
    cols = [
        r[0]
        for r in con.execute(
            "SELECT column_name FROM duckdb_columns() WHERE table_name = 'features' "
            "ORDER BY column_index"
        ).fetchall()
    ]
    select = ", ".join("ST_AsText(bbox)" if c == "bbox" else f'"{c}"' for c in cols)
    # As a set: GTF synthesis inserts the inferred rows in whatever order
    # DuckDB's threads produce them, which nothing reads.
    out["features"] = sorted(map(repr, con.execute(f"SELECT {select} FROM features").fetchall()))
    # Grouped by feature, in insertion order within each: that order is
    # observable (a merged feature's blob is rebuilt `ORDER BY rowid`).
    rows = con.execute("SELECT * FROM attributes ORDER BY rowid").fetchall()
    out["attributes"] = sorted(rows, key=lambda r: r[0])
    for table in SORTED:
        out[table] = sorted(map(repr, con.execute(f"SELECT * FROM {table}").fetchall()))
    return out


def _build(path: str, kwargs: dict, native: bool, monkeypatch):
    monkeypatch.setattr(ingest, "_USE_NATIVE_PRODUCER", native)
    try:
        con, stats = ingest.from_file(path, ":memory:", options=IngestOptions(**kwargs))
    except Exception as exc:  # noqa: BLE001 - the exception is the result
        return exc
    return (
        _dump(con),
        stats.n_features_raw,
        stats.n_skipped,
        stats.n_multipart,
        stats.warnings,
        stats.dialect,
        stats.directives,
    )


@pytest.mark.parametrize("option", sorted(OPTIONS))
@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_producer_builds_the_same_database(fixture, option, monkeypatch):
    path = os.path.join(DATA, fixture)
    python = _build(path, OPTIONS[option], False, monkeypatch)
    rust = _build(path, OPTIONS[option], True, monkeypatch)
    if isinstance(python, Exception) or isinstance(rust, Exception):
        assert type(rust) is type(python), f"{rust!r} vs {python!r}"
        # The producer adds line numbers to a duplicate-id message.
        assert str(rust).startswith(str(python)), f"{rust} vs {python}"
        return
    assert rust == python


#: Inputs no fixture covers, each a rule the producer reimplements.
NOISY = {
    "augustus": (
        "chr1\tAUGUSTUS\tgene\t100\t900\t0.9\t+\t.\tg1\n"
        "chr1\tAUGUSTUS\ttranscript\t100\t900\t0.9\t+\t.\tg1.t1\n"
        'chr1\tAUGUSTUS\tCDS\t100\t300\t0.8\t+\t0\ttranscript_id "g1.t1"; gene_id "g1";\n'
    ),
    "bare_token_not_alone": (
        # Not the whole column (a trailing note), so not a bare id.
        "chr1\tAUGUSTUS\tgene\t100\t900\t.\t+\t.\tg1 x\n"
        "chr1\tAUGUSTUS\ttranscript\t100\t900\t.\t+\t.\tg1.t1;;\n"
        'chr1\tAUGUSTUS\tCDS\t100\t300\t.\t+\t0\ttranscript_id "g1.t1"; gene_id "g1";\n'
    ),
    "gtf_without_exons_or_cds": (
        'chr1\tt\tstart_codon\t1\t3\t.\t+\t0\tgene_id "G"; transcript_id "T";\n'
    ),
    "gtf_merge": (
        'chr1\tt\texon\t1\t10\t.\t+\t.\tgene_id "G"; transcript_id "T"; note "a";\n'
        'chr1\tt\texon\t1\t10\t.\t+\t.\tgene_id "G"; transcript_id "T"; note "b";\n'
    ),
    "liftoff": (
        "chr1\tt\tgene\t1\t9\t.\t+\t.\tID=X_1\n"
        "chr1\tt\tgene\t20\t29\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t40\t49\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t60\t69\t.\t+\t.\tID=X_2\n"
    ),
    "generated_ids": (
        "chr1\tt\texon\t1\t9\t.\t+\t.\tID=exon_1\n"
        "chr1\tt\texon\t20\t29\t.\t+\t.\tName=a\n"
        "chr1\tt\texon\t40\t49\t.\t+\t.\tName=b\n"
        "chr1\tt\texon\t60\t69\t.\t+\t.\tID=exon_3\n"
    ),
    "empty_and_multivalued": (
        "chr1\tt\tgene\t1\t9\t.\t+\t.\tID=g;Note=;Parent=a,;Alias=,b\n"
        "chr1\tt\tgene\t20\t29\t.\t+\t.\tID=;Name=n\n"
    ),
    "extra_columns_and_dots": (
        "chr1\tt\tgene\t.\t.\t.\t.\t.\tID=g\textra1\textra2\nchr2\tt\tgene\t5\t9\t.\t-\t.\t.\n"
    ),
}

NOISY_OPTIONS = {
    "default": {},
    "create_unique": {"merge_strategy": "create_unique"},
    "merge": {"merge_strategy": "merge"},
    "replace": {"merge_strategy": "replace"},
    "warning": {"merge_strategy": "warning"},
    "strict": {"mode": "strict", "on_multipart_conflict": "split"},
    "gene_id": {"id_spec": {"exon": "gene_id"}, "merge_strategy": "merge"},
}


@pytest.mark.parametrize("option", sorted(NOISY_OPTIONS))
@pytest.mark.parametrize("name", sorted(NOISY))
def test_the_producer_agrees_on_noisy_input(name, option, monkeypatch, tmp_path):
    path = tmp_path / ("in.gtf" if "gtf" in name or "augustus" in name else "in.gff3")
    path.write_text(NOISY[name])
    python = _build(str(path), NOISY_OPTIONS[option], False, monkeypatch)
    rust = _build(str(path), NOISY_OPTIONS[option], True, monkeypatch)
    if isinstance(python, Exception) or isinstance(rust, Exception):
        assert type(rust) is type(python), f"{rust!r} vs {python!r}"
        assert str(rust).startswith(str(python)), f"{rust} vs {python}"
        return
    assert rust == python


@pytest.mark.parametrize("native", [False, True])
def test_progress_is_logged_on_both_paths(native, monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setattr(ingest, "_USE_NATIVE_PRODUCER", native)
    monkeypatch.setattr(ingest, "_PROGRESS_EVERY", 1)
    path = tmp_path / "a.gff3"
    path.write_text("chr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1\n" * 1)
    with caplog.at_level(logging.INFO, logger="gffbase.ingest"):
        ingest.from_file(str(path), ":memory:", batch_size=1)
    assert any("records parsed" in r.getMessage() for r in caplog.records)


def test_the_producer_is_used_where_it_can_be(monkeypatch, tmp_path):
    calls = []
    real = ingest._native_producer

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        calls.append(result is not None)
        return result

    monkeypatch.setattr(ingest, "_native_producer", spy)
    path = tmp_path / "a.gff3"
    path.write_text("chr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1\n")
    ingest.from_file(str(path), ":memory:")
    ingest.from_file(str(path), ":memory:", options=IngestOptions(transform=lambda f: f))
    ingest.from_file(str(path), ":memory:", options=IngestOptions(id_spec=lambda f: "x"))
    assert calls == [True, False, False]


def test_a_duplicate_id_error_names_both_lines(tmp_path):
    from gffbase import DuplicateIDError

    path = tmp_path / "dup.gff3"
    path.write_text(
        "##gff-version 3\n"
        "chr1\tt\tgene\t1\t10\t.\t+\t.\tID=g1\n"
        "chr1\tt\tgene\t20\t30\t.\t+\t.\tID=g2\n"
        "chr1\tt\tgene\t40\t50\t.\t+\t.\tID=g1\n"
    )
    with pytest.raises(DuplicateIDError, match=r"Duplicate ID g1 \(line 4, first seen on line 2\)"):
        ingest.from_file(str(path), ":memory:")
