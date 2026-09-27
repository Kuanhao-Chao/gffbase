"""`merge` / `replace` resolution in bulk decides exactly what row by row did.

`ingest._resolve_deferred_duplicates` used to resolve each held-back
duplicate with its own queries and, for a rename, its own INSERT. The
attributes table has no index at that point, so every duplicate cost a scan:
hours on a split-CDS corpus. The bulk version keeps the decisions -- same
file order, same comparisons, same names -- and does the I/O in bulk. The
row-by-row pass is kept here, verbatim but for its imports, as the oracle.
"""

from __future__ import annotations

import os

import pytest
from gffbase import ingest
from gffbase._dbutil import scalar
from gffbase._options import IngestOptions

from tests.test_ingest_producer_equivalence import _dump

DATA = os.environ.get("GFFBASE_GFFUTILS_DATA", "/ccb/salz3/kh.chao/gffutils/gffutils/test/data")
FIXTURES = sorted(
    f
    for f in (os.listdir(DATA) if os.path.isdir(DATA) else [])
    if not f.endswith((".db", ".sqlite", ".fa", ".fasta", ".bed", ".py", ".json", ".csv"))
)


def _row_by_row(con, deferred, options, autoinc, builder, seqid_to_y, fmt):
    """The 0.2.1 pass: one feature at a time, one query at a time."""
    q, as_text = ingest._quote, ingest._as_text
    fields = ingest._MERGE_COMPARE_FIELDS
    new_duplicates = []
    compare = [f for f in fields if f not in options.force_merge_fields]

    def insert_single(fid, feat, file_order):
        builder.append(fid, feat, file_order)
        builder.flush_into(con)

    def regenerate(feature_id):
        rows = con.execute(
            "SELECT key, value FROM attributes WHERE feature_id = ? ORDER BY rowid",
            [feature_id],
        ).fetchall()
        blob = ingest._render_attributes_blob(rows, fmt)
        if blob is not None:
            con.execute("UPDATE features SET attributes_blob = ? WHERE id = ?", [blob, feature_id])

    for fid, feat, file_order in deferred:
        if options.merge_strategy == "replace":
            con.execute("DELETE FROM attributes WHERE feature_id = ?", [fid])
            con.execute("DELETE FROM features WHERE id = ?", [fid])
            insert_single(fid, feat, file_order)
            continue
        row = con.execute(
            f"SELECT {', '.join(q(f) for f in fields)} FROM features WHERE id = ?", [fid]
        ).fetchone()
        existing = dict(zip(fields, row, strict=True)) if row else {}
        same = bool(existing) and all(
            as_text(existing[f]) == as_text(getattr(feat, f)) for f in compare
        )
        if not same:
            new_id = ingest._free_autoincrement(con, fid, autoinc)
            insert_single(new_id, feat, file_order)
            new_duplicates.append((fid, new_id))
            continue
        for key, value, idx in feat.attributes_pairs:
            already = scalar(
                con,
                "SELECT COUNT(*) FROM attributes WHERE feature_id = ? AND key = ? AND value = ?",
                [fid, key, value],
            )
            if not already:
                con.execute(
                    "INSERT INTO attributes (feature_id, key, value, idx) VALUES (?, ?, ?, ?)",
                    [fid, key, value, idx],
                )
        for fld in options.force_merge_fields:
            merged = sorted({as_text(existing[fld]), as_text(getattr(feat, fld))})
            con.execute(f"UPDATE features SET {q(fld)} = ? WHERE id = ?", [",".join(merged), fid])
        regenerate(fid)
    return new_duplicates


def _build(path, kwargs, resolver, monkeypatch):
    monkeypatch.setattr(ingest, "_resolve_deferred_duplicates", resolver)
    try:
        con, stats = ingest.from_file(path, ":memory:", options=IngestOptions(**kwargs))
    except Exception as exc:  # noqa: BLE001 - the exception is the result
        return exc
    return _dump(con), stats.n_features_raw, stats.warnings


def _agree(path, kwargs, monkeypatch):
    bulk = _build(path, kwargs, ingest._resolve_deferred_duplicates, monkeypatch)
    oracle = _build(path, kwargs, _row_by_row, monkeypatch)
    if isinstance(oracle, Exception) or isinstance(bulk, Exception):
        assert type(bulk) is type(oracle), f"{bulk!r} vs {oracle!r}"
        return
    assert bulk == oracle


@pytest.mark.parametrize("strategy", ["merge", "replace"])
@pytest.mark.parametrize("fixture", FIXTURES)
def test_bulk_matches_row_by_row_on_every_fixture(fixture, strategy, monkeypatch):
    _agree(os.path.join(DATA, fixture), {"merge_strategy": strategy}, monkeypatch)


CASES = {
    "merge_same_and_different": (
        "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Note=a\n"
        "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Note=b;Note=a\n"
        "chr1\tt\tgene\t5\t100\t.\t+\t.\tID=g1;Note=c\n"
        "chr1\tt\tgene\t1\t100\t.\t+\t.\tID=g1;Alias=x,y\n"
        "chr1\tt\tgene\t9\t100\t.\t+\t.\tID=g1\n"
    ),
    "rename_collides_with_literal_ids": (
        "chr1\tt\tgene\t1\t9\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t20\t29\t.\t+\t.\tID=X_1\n"
        "chr1\tt\tgene\t40\t49\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t60\t69\t.\t+\t.\tID=X\n"
        "chr1\tt\tgene\t80\t89\t.\t+\t.\tID=X_3\n"
    ),
    "escaped_merged_value": (
        "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Note=first%3Bsemi\n"
        "chr1\tsrc\tgene\t1\t100\t.\t+\t.\tID=g1;Note=second\n"
    ),
    "many_renames_past_the_headroom": "".join(
        f"chr1\tt\tCDS\t{i * 10 + 1}\t{i * 10 + 5}\t.\t+\t0\tID=cds1\n" for i in range(40)
    )
    + "".join(f"chr1\tt\tgene\t{i}\t{i}\t.\t+\t.\tID=cds1_{i}\n" for i in range(20, 45)),
    "gtf": (
        'chr1\tt\texon\t1\t10\t.\t+\t.\tgene_id "G"; transcript_id "T"; note "a";\n'
        'chr1\tt\texon\t1\t10\t.\t+\t.\tgene_id "G"; transcript_id "T"; note "b";\n'
    ),
}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"merge_strategy": "merge"},
        {"merge_strategy": "replace"},
        {"merge_strategy": "merge", "force_merge_fields": ["source"]},
        {"merge_strategy": "merge", "id_spec": {"exon": "gene_id"}},
    ],
    ids=["merge", "replace", "force_source", "gtf_ids"],
)
@pytest.mark.parametrize("case", sorted(CASES))
def test_bulk_matches_row_by_row_on_hard_cases(case, kwargs, monkeypatch, tmp_path):
    path = tmp_path / ("in.gtf" if case == "gtf" else "in.gff3")
    path.write_text(CASES[case])
    _agree(str(path), kwargs, monkeypatch)
