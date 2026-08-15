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
"""``create_db()`` — drop-in successor to ``gffutils.create_db``.

The parameter list, its order, and the defaults all match gffutils, so calls
written against it -- including positional ones -- work unchanged. Every
parameter is honoured; none is accepted and ignored.
"""

from __future__ import annotations

import os
import tempfile

from gffbase import ingest as _ingest
from gffbase._options import IngestOptions
from gffbase.exceptions import EmptyInputError
from gffbase.interface import FeatureDB


def _render_features(features) -> str:
    """Serialize an iterable of features into GFF text.

    Directives are not emitted: a feature iterable carries none, and inventing
    a `##gff-version` line would let the format be inferred from something the
    caller never supplied.
    """
    lines = []
    for feature in features:
        line = str(feature)
        lines.append(line if line.endswith("\n") else line + "\n")
    if not lines:
        raise EmptyInputError("cannot build a database from an empty feature iterable")
    return "".join(lines)


def create_db(
    data,
    dbfn,
    id_spec=None,
    force=False,
    verbose=False,
    checklines=10,
    merge_strategy="error",
    transform=None,
    gtf_transcript_key="transcript_id",
    gtf_gene_key="gene_id",
    gtf_subfeature="exon",
    force_gff=False,
    force_dialect_check=False,
    from_string=False,
    keep_order=False,
    text_factory=str,
    force_merge_fields=None,
    pragmas=None,
    sort_attribute_values=False,
    dialect=None,
    _keep_tempfiles=False,
    infer_gene_extent=True,
    disable_infer_genes=False,
    disable_infer_transcripts=False,
    mode="compat",
    validation=None,
    on_error=None,
    on_multipart_conflict="error",
    **kwargs,
) -> FeatureDB:
    """Create a database from a GFF3/GTF source.

    Parameters
    ----------
    data :
        Path to a GFF3/GTF file (optionally gzipped), or the file contents
        themselves when ``from_string=True``.
    dbfn :
        Destination database path, or ``":memory:"``.
    id_spec :
        Primary-key policy. ``None`` uses the per-dialect default: ``"ID"`` for
        GFF3, ``{"gene": "gene_id", "transcript": "transcript_id"}`` for GTF.
        May also be an attribute name, an ordered list of names (first match
        wins), a ``featuretype -> name`` mapping, or a callable returning an id
        (or ``"autoincrement:BASE"``). A name of the form ``":seqid:"`` reads
        that GFF column instead of an attribute.
    force :
        Overwrite ``dbfn`` if it exists. Without it, an existing file raises.
    verbose :
        Progress reporting. ``"debug"`` selects DEBUG level.
    checklines :
        Lines sampled to infer the dialect.
    merge_strategy :
        What to do when two features resolve to the same id: ``"error"``,
        ``"warning"``, ``"merge"``, ``"create_unique"`` or ``"replace"``.
    transform :
        Callable applied to each feature. Returning anything falsy drops it.
    gtf_transcript_key, gtf_gene_key, gtf_subfeature :
        Attribute names used to reconstruct GTF hierarchy.
    force_gff :
        Skip format autodetection and treat the input as GFF.
    force_dialect_check :
        Infer the dialect from every line rather than the first ``checklines``.
        Mutually exclusive with ``dialect``.
    from_string :
        Treat ``data`` as file contents rather than a path.
    keep_order :
        Preserve attribute order when features are rendered.
    text_factory :
        Text coercion applied to values read back out.
    force_merge_fields :
        With ``merge_strategy="merge"``, fields allowed to differ and be
        combined. ``start``/``end`` are rejected: they must stay numeric.
    pragmas :
        Database pragmas.
    sort_attribute_values :
        Sort attribute values when features are rendered.
    dialect :
        Explicit dialect, bypassing inference.
    infer_gene_extent :
        Deprecated. ``False`` sets both ``disable_infer_*`` flags.
    disable_infer_genes, disable_infer_transcripts :
        Skip synthesizing GTF gene/transcript rows from their children.
    mode : {"compat", "strict"}
        ``"compat"`` (default) applies gffutils' rule set, so files that break
        the GFF3 specification load exactly as they do under gffutils, with
        every violation recorded in ``FeatureDB.warnings``. ``"strict"``
        applies the full NCBI specification and rejects violations.

        ``"strict"`` is also the only mode that fuses several lines sharing one
        ``ID`` into a single discontinuous feature. That is deliberate: it is
        new behaviour rather than gffutils behaviour, since gffutils'
        ``merge_strategy="merge"`` requires all eight non-attribute columns to
        match and so never merges a genuine split feature.
    validation : {"gffutils", "ncbi"} | None
        Which rule set to apply, overriding the one ``mode`` implies. ``None``
        (default) takes the mode's. Use it to keep gffutils-compatible
        *handling* while applying the full NCBI *rules* -- the combination
        ``validation="ncbi", on_error="warn"`` audits a file without stopping
        on it, leaving every violation in ``FeatureDB.warnings``.
    on_error : {"raise", "warn"} | None
        What a rejected line does, overriding the one ``mode`` implies.
        ``"raise"`` stops at the first violation; ``"warn"`` records it and
        carries on. ``None`` (default) takes the mode's.
    on_multipart_conflict : {"error", "split"}
        Under ``mode="strict"``, what to do when lines sharing an ``ID``
        disagree on seqid, source, featuretype or strand -- which GFF3 requires
        the segments of a discontinuous feature to share. ``"error"`` (default)
        raises :class:`~gffbase.exceptions.MultipartConstraintError` naming the
        diverging column and both line numbers; ``"split"`` partitions the run
        by those four columns, the lowest ``file_order`` keeping the bare id.

    Returns
    -------
    FeatureDB
    """
    if kwargs:
        # gffutils' `deprecation_handler` does the same: an unrecognized
        # keyword is a caller error, not something to absorb silently.
        raise TypeError(f"unhandled kwarg in {sorted(kwargs)}")

    # Same guard as `FeatureDB.__init__`, and for the same reason: DuckDB takes
    # the path as a C string and stops at a NUL, so the database would be
    # created somewhere other than the path the caller named. Checking here as
    # well because `create_db` reaches the filesystem (the `force=True` unlink)
    # before it ever constructs a `FeatureDB`.
    if isinstance(dbfn, (str, os.PathLike)) and "\x00" in os.fspath(dbfn):
        raise ValueError(
            f"database path contains an embedded NUL byte: {os.fspath(dbfn)!r}. "
            "Paths are passed to DuckDB as C strings, which would silently "
            "truncate at the NUL and create a different file."
        )

    options = IngestOptions(
        id_spec=id_spec,
        force=force,
        verbose=verbose,
        checklines=checklines,
        merge_strategy=merge_strategy,
        on_multipart_conflict=on_multipart_conflict,
        transform=transform,
        gtf_transcript_key=gtf_transcript_key,
        gtf_gene_key=gtf_gene_key,
        gtf_subfeature=gtf_subfeature,
        force_gff=force_gff,
        force_dialect_check=force_dialect_check,
        from_string=from_string,
        keep_order=keep_order,
        text_factory=text_factory,
        force_merge_fields=force_merge_fields,
        pragmas=pragmas,
        sort_attribute_values=sort_attribute_values,
        dialect=dialect,
        keep_tempfiles=_keep_tempfiles,
        infer_gene_extent=infer_gene_extent,
        disable_infer_genes=disable_infer_genes,
        disable_infer_transcripts=disable_infer_transcripts,
        mode=mode,
        validation=validation,
        on_error=on_error,
    )

    # An iterable of features is a documented input: `helpers.sanitize_gff_db`
    # builds a database out of a generator, and the oracle's `_FeatureIterator`
    # exists for exactly this. Render it and take the `from_string` path, which
    # is the honest route here -- the ingest pipeline is file-oriented all the
    # way down to the Rust parser, so "accepting features directly" would mean
    # serializing them anyway, just less visibly.
    if not from_string and not isinstance(data, (str, bytes, os.PathLike)):
        data = _render_features(data)
        from_string = True

    cleanup_path: str | None = None
    if from_string:
        # `_keep_tempfiles` may be a string, in which case gffutils uses it as
        # the suffix -- handy for telling parallel runs apart.
        suffix = _keep_tempfiles if isinstance(_keep_tempfiles, str) else ".gff3"
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8")
        tmp.write(data)
        tmp.close()
        path = tmp.name
        if not _keep_tempfiles:
            cleanup_path = path
    else:
        path = os.fspath(data) if hasattr(data, "__fspath__") else data

    try:
        con, stats = _ingest.from_file(path, dbfn=dbfn, options=options)
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass

    return FeatureDB(
        (con, stats),
        keep_order=keep_order,
        sort_attribute_values=sort_attribute_values,
        text_factory=text_factory,
        pragmas=pragmas,
        # `con` was opened by the ingest above and nobody else holds it, so
        # the returned handle owns it. Without this, `with create_db(...) as
        # db:` would exit without releasing the connection it created --
        # exactly the leak the context manager exists to prevent.
        _own_conn=True,
    )
