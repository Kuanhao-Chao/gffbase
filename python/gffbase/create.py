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
"""Database-construction seam, under the names gffutils uses.

The oracle's `_DBCreator` / `_GFFDBCreator` / `_GTFDBCreator` are
underscore-named but load-bearing: `FeatureDB.__init__` accepts one as
`dbfn`, and downstream code subclasses them to hook ingestion. gffbase's
ingestion is a Rust parser feeding Arrow batches into DuckDB and has no
per-row Python seam to subclass, so these are **adapters, not ports** -- they
carry the constructor signature and expose `create()`, which runs the real
pipeline.

Anything that subclasses `_GFFDBCreator` to override a row-level hook will
not work here, and cannot: there is no row-level Python hook to override. Use
`create_db(transform=...)`, which is the supported way to intervene per
feature and is what most such subclasses were doing.
"""

from __future__ import annotations

__all__ = ["_DBCreator", "_GFFDBCreator", "_GTFDBCreator", "create_db"]

from gffbase.create_db import create_db


class _DBCreator:
    """Adapter over `create_db`, with the oracle's constructor signature."""

    def __init__(
        self,
        data,
        dbfn,
        force=False,
        verbose=False,
        id_spec=None,
        merge_strategy="error",
        checklines=10,
        transform=None,
        force_dialect_check=False,
        from_string=False,
        dialect=None,
        default_encoding="utf-8",
        disable_infer_genes=False,
        disable_infer_transcripts=False,
        infer_gene_extent=True,
        force_merge_fields=None,
        text_factory=str,
        **kwargs,
    ):
        self.data = data
        self.dbfn = dbfn
        self.kwargs = dict(
            force=force,
            verbose=verbose,
            id_spec=id_spec,
            merge_strategy=merge_strategy,
            checklines=checklines,
            transform=transform,
            force_dialect_check=force_dialect_check,
            from_string=from_string,
            dialect=dialect,
            disable_infer_genes=disable_infer_genes,
            disable_infer_transcripts=disable_infer_transcripts,
            infer_gene_extent=infer_gene_extent,
            force_merge_fields=force_merge_fields,
            text_factory=text_factory,
        )
        self.kwargs.update(kwargs)
        self.default_encoding = default_encoding
        self._db = None

    def create(self):
        """Build the database and return the `FeatureDB`."""
        self._db = create_db(self.data, self.dbfn, **self.kwargs)
        return self._db

    @property
    def db(self):
        if self._db is None:
            self.create()
        return self._db


class _GFFDBCreator(_DBCreator):
    """GFF3 specialization. `force_gff=True` pins the format."""

    def create(self):
        self._db = create_db(self.data, self.dbfn, force_gff=True, **self.kwargs)
        return self._db


class _GTFDBCreator(_DBCreator):
    """GTF specialization, carrying the three GTF-only knobs."""

    def __init__(self, *args, **kwargs):
        self.transcript_key = kwargs.pop("transcript_key", "transcript_id")
        self.gene_key = kwargs.pop("gene_key", "gene_id")
        self.subfeature = kwargs.pop("subfeature", "exon")
        super().__init__(*args, **kwargs)

    def create(self):
        self._db = create_db(
            self.data,
            self.dbfn,
            gtf_transcript_key=self.transcript_key,
            gtf_gene_key=self.gene_key,
            gtf_subfeature=self.subfeature,
            **self.kwargs,
        )
        return self._db


def deprecation_handler(kwargs: dict) -> dict:
    """Normalize keyword arguments whose names changed across versions.

    `create_db` rejects unknown keywords outright, so this is the seam where a
    renamed one is translated first. Returns the kwargs it was given, mutated
    in place, matching the oracle's contract.
    """
    if "infer_gene_extent" in kwargs:
        # Superseded by the two independent switches; kept working because it
        # appears in published examples.
        extent = kwargs.pop("infer_gene_extent")
        kwargs.setdefault("disable_infer_genes", not extent)
        kwargs.setdefault("disable_infer_transcripts", not extent)
    return kwargs
