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
"""Ingestion policy: one validated object carrying every ``create_db`` option.

Before this existed, ``create_db`` accepted twelve parameters and silently
ignored them -- ``id_spec``, ``merge_strategy``, ``transform`` and the rest
were part of the signature but not part of the behaviour, so a caller porting
from gffutils got a database that quietly disagreed with the one they asked
for. Every option now either changes what happens or raises.

The two substantial pieces are :class:`IngestOptions`, which validates the
whole option set up front, and :class:`IdSpecResolver`, which implements
gffutils' primary-key policy.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gffbase.feature import _drop_lone_empty_values
from gffbase.modes import MODE_STRICT, ResolvedMode, resolve_mode

#: The five duplicate-ID policies gffutils accepts.
MERGE_STRATEGIES = ("error", "warning", "merge", "create_unique", "replace")

#: What `mode="strict"` does when lines sharing one `ID` cannot be one
#: discontinuous feature -- when they disagree on seqid, source, featuretype
#: or strand, which GFF3 requires them to share.
MULTIPART_CONFLICT_ACTIONS = ("error", "split")

#: Non-attribute fields that ``force_merge_fields`` may name. `start` and `end`
#: are deliberately absent: merging them would mean joining integers into a
#: string, and the coordinate columns must stay numeric.
MERGEABLE_FIELDS = ("seqid", "source", "featuretype", "score", "strand", "frame")

#: gffutils' default primary-key policy, per dialect.
DEFAULT_ID_SPEC_GFF3 = "ID"
DEFAULT_ID_SPEC_GTF: dict[str, str] = {"gene": "gene_id", "transcript": "transcript_id"}

_AUTOINCREMENT_PREFIX = "autoincrement:"


class _FeatureAdapter:
    """gffutils-``Feature``-shaped view over a ``ParsedFeature``.

    ``transform`` and callable ``id_spec`` functions are written against
    gffutils, so they expect ``f.attributes[key]`` to be a *list*, the nine
    GFF columns to be plain attributes, and the rest of the gffutils surface:
    ``f.id`` (``None`` -- the id is assigned after the callable runs),
    ``f.chrom`` / ``f.stop``, ``f.dialect``, ``len(f)``, ``str(f)``,
    ``f[key] = value``. Building a full :class:`~gffbase.feature.Feature` per
    row would cost a dict materialization per line at GENCODE scale, so this
    is built only when a callable actually runs.

    Attribute edits are kept: the first read of ``attributes`` snapshots it,
    and ingest (`ingest._unwrap_transformed`) writes a changed mapping back
    into the record. They used to be discarded -- a transform renaming a gene
    or adding a key returned "success" and stored the original.
    """

    __slots__ = ("_parsed", "_attributes", "_snapshot", "_id", "_dialect")

    _parsed: Any
    _attributes: dict[str, list[str]] | None
    _snapshot: dict[str, list[str]] | None
    _id: Any
    _dialect: dict | None

    _OWN = frozenset(__slots__)
    #: gffutils' names for two columns.
    _ALIASES = {"chrom": "seqid", "stop": "end"}

    def __init__(self, parsed, dialect: dict | None = None):
        object.__setattr__(self, "_parsed", parsed)
        object.__setattr__(self, "_attributes", None)
        object.__setattr__(self, "_snapshot", None)
        object.__setattr__(self, "_id", None)
        object.__setattr__(self, "_dialect", dialect)

    def __getattr__(self, name):
        # Only reached when normal lookup fails, i.e. for the wrapped
        # feature's own fields.
        return getattr(self._parsed, name)

    def __setattr__(self, name, value):
        """Write through to the wrapped feature.

        gffutils transforms mutate the feature in place -- `f.source = "x"`
        then `return f` -- so the adapter has to be writable, not just
        readable, or every such transform dies with AttributeError.
        """
        if name in self._OWN:
            object.__setattr__(self, name, value)
        elif name == "id":
            # gffutils assigns the id from `id_spec` after the transform, so
            # setting it here has no effect there either; it is kept only so
            # the transform can read back what it wrote.
            object.__setattr__(self, "_id", value)
        elif name == "attributes":
            object.__setattr__(self, "_attributes", self._normalize(value))
            if self._snapshot is None:
                object.__setattr__(self, "_snapshot", self._pairs_dict())
        else:
            setattr(self._parsed, self._ALIASES.get(name, name), value)

    def __bool__(self) -> bool:
        # `__len__` below is the feature's length; without this a feature
        # with no coordinates would be falsy, and ingest reads a falsy
        # transform result as "drop this record".
        return True

    def __len__(self) -> int:
        start, end = self._parsed.start, self._parsed.end
        return 0 if start is None or end is None else end - start + 1

    @property
    def id(self):
        return self._id

    @property
    def dialect(self) -> dict:
        return dict(self._dialect) if self._dialect else {"fmt": "gff3"}

    @property
    def chrom(self) -> str:
        return self._parsed.seqid

    @property
    def stop(self):
        return self._parsed.end

    def _pairs_dict(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for key, value, _idx in self._parsed.attributes_pairs:
            out.setdefault(key, []).append(value)
        _drop_lone_empty_values(out)
        return out

    @staticmethod
    def _normalize(mapping) -> dict[str, list[str]]:
        # gffutils' Attributes wraps a bare value in a list on assignment.
        return {
            k: list(v) if isinstance(v, list | tuple) else [v] for k, v in dict(mapping).items()
        }

    @property
    def attributes(self) -> dict[str, list[str]]:
        if self._attributes is None:
            object.__setattr__(self, "_attributes", self._pairs_dict())
            object.__setattr__(self, "_snapshot", self._pairs_dict())
        return self._attributes  # type: ignore[return-value]

    def edited_attributes(self) -> dict[str, list[str]] | None:
        """The attribute mapping if the callable changed it, else None."""
        if self._attributes is None:
            return None
        current = self._normalize(self._attributes)
        return None if current == self._snapshot else current

    def __getitem__(self, key):
        return self.attributes[key]

    def __setitem__(self, key, value) -> None:
        self.attributes[key] = list(value) if isinstance(value, list | tuple) else [value]

    def __str__(self) -> str:
        from gffbase._serialize import _reconstruct

        edited = self.edited_attributes()
        col9 = (
            self._parsed.attributes_blob.decode("utf-8", errors="replace")
            if edited is None
            else _reconstruct(edited, self.dialect)
        )
        p = self._parsed
        cols = [
            p.seqid,
            p.source,
            p.featuretype,
            "." if p.start is None else str(p.start),
            "." if p.end is None else str(p.end),
            p.score,
            p.strand,
            p.frame,
            col9,
            *p.extra,
        ]
        return "\t".join(cols)


def _is_callable(obj: Any) -> bool:
    return callable(obj)


class IdSpecResolver:
    """Resolve a feature's primary key, mirroring gffutils' ``_id_handler``.

    Supported forms, in the order gffutils checks them:

    ``str``
        A single attribute key.
    callable
        Receives the feature; a falsy return falls through to autoincrement.
        A return of ``"autoincrement:BASE"`` autoincrements on ``BASE``.
    ``dict``
        ``featuretype -> key`` (or ``-> [keys]``). A featuretype absent from
        the mapping autoincrements immediately, *without* trying other keys.
    iterable
        Keys tried in order; the first that yields a value wins.

    A key of the form ``:name:`` reads GFF column ``name`` rather than an
    attribute, so ``":seqid:"`` keys features on their sequence name.
    """

    __slots__ = ("id_spec", "_uses_callable")

    def __init__(self, id_spec):
        self.id_spec = id_spec
        self._uses_callable = self._detect_callable(id_spec)

    @staticmethod
    def _detect_callable(id_spec) -> bool:
        if _is_callable(id_spec):
            return True
        if isinstance(id_spec, dict):
            return any(
                _is_callable(v) or (not isinstance(v, str) and any(map(_is_callable, v)))
                for v in id_spec.values()
            )
        if isinstance(id_spec, list | tuple):
            return any(map(_is_callable, id_spec))
        return False

    @property
    def uses_callable(self) -> bool:
        """Whether resolving needs a Feature-shaped object built per row."""
        return self._uses_callable

    def resolve(self, parsed, autoincrements: dict[str, int]) -> tuple[str, str]:
        """Return ``(id, origin)`` for one parsed feature.

        ``origin`` is ``"attribute"`` when the id came from the data and
        ``"autoincrement"`` when it was generated, which lets the caller
        populate the ``autoincrements`` bookkeeping table the way gffutils does.
        """
        keys = self._keys_for(parsed, autoincrements)
        if keys is None:
            # A dict id_spec with no entry for this featuretype: gffutils
            # autoincrements immediately rather than falling through.
            return self._autoincrement(parsed.featuretype, autoincrements), "autoincrement"

        for key in keys:
            if _is_callable(key):
                value = key(_FeatureAdapter(parsed))
                if not value:
                    continue
                if value.startswith(_AUTOINCREMENT_PREFIX):
                    base = value[len(_AUTOINCREMENT_PREFIX) :]
                    return self._autoincrement(base, autoincrements), "autoincrement"
                return value, "attribute"

            # `:name:` reads a GFF column instead of an attribute. Columns are
            # scalars, so unlike attributes there is no `[0]` to take.
            if len(key) > 3 and key[0] == ":" and key[-1] == ":":
                return str(getattr(parsed, key[1:-1])), "attribute"

            values = [v for k, v, _idx in parsed.attributes_pairs if k == key]
            if len(values) > 1:
                raise ValueError(
                    f"The ID field {key} has more than one value but a single "
                    "value is required for a primary key in the database. "
                    "Consider using a custom id_spec to convert these multiple "
                    "values into a single value"
                )
            # An empty value (`ID=`) is treated as absent, matching gffutils:
            # its attribute parser yields an empty list for such a key, so the
            # `[0]` lookup raises IndexError and falls through to the next key.
            if values and values[0]:
                return values[0], "attribute"

        return self._autoincrement(parsed.featuretype, autoincrements), "autoincrement"

    def _keys_for(self, parsed, autoincrements):
        spec = self.id_spec
        if isinstance(spec, str):
            return [spec]
        if _is_callable(spec):
            return [spec]
        if isinstance(spec, dict):
            try:
                key = spec[parsed.featuretype]
            except KeyError:
                return None
            return [key] if isinstance(key, str) or _is_callable(key) else list(key)
        return list(spec)

    @staticmethod
    def _autoincrement(base: str, autoincrements: dict[str, int]) -> str:
        n = autoincrements.get(base, 0) + 1
        autoincrements[base] = n
        return f"{base}_{n}"


@dataclass
class IngestOptions:
    """Every ``create_db`` option, validated once at construction.

    Validation happens here rather than at each use site so that a bad option
    is reported before any work starts -- in particular before the destination
    database is touched.
    """

    id_spec: Any = None
    force: bool = False
    verbose: Any = False
    checklines: int = 10
    merge_strategy: str = "error"
    transform: Callable | None = None
    gtf_transcript_key: str = "transcript_id"
    gtf_gene_key: str = "gene_id"
    gtf_subfeature: str = "exon"
    force_gff: bool = False
    force_dialect_check: bool = False
    from_string: bool = False
    keep_order: bool = False
    text_factory: Any = str
    force_merge_fields: list[str] = field(default_factory=list)
    pragmas: dict | None = None
    sort_attribute_values: bool = False
    dialect: dict | None = None
    keep_tempfiles: Any = False
    infer_gene_extent: bool = True
    disable_infer_genes: bool = False
    disable_infer_transcripts: bool = False
    #: Compatibility vs standards. `create_db` defaults to "compat", which is
    #: what lets it read the files gffutils reads.
    mode: str = "compat"
    validation: str | None = None
    on_error: str | None = None
    #: What to do when lines sharing an `ID` disagree on a column GFF3 requires
    #: the segments of a discontinuous feature to share. Consulted only under
    #: `mode="strict"`, which is the only mode that fuses at all.
    on_multipart_conflict: str = "error"
    resolved_mode: ResolvedMode = field(init=False)

    def __post_init__(self) -> None:
        self.resolved_mode = resolve_mode(
            self.mode, validation=self.validation, on_error=self.on_error
        )

        if self.merge_strategy not in MERGE_STRATEGIES:
            raise ValueError(f"Invalid merge strategy '{self.merge_strategy}'")

        if self.on_multipart_conflict not in MULTIPART_CONFLICT_ACTIONS:
            raise ValueError(
                f"Invalid on_multipart_conflict '{self.on_multipart_conflict}'; "
                f"expected one of {MULTIPART_CONFLICT_ACTIONS}"
            )

        self.force_merge_fields = list(self.force_merge_fields or [])
        for name in self.force_merge_fields:
            if name in ("start", "end"):
                raise ValueError("Can't merge start/end fields since they must be integers")
            if name not in MERGEABLE_FIELDS:
                raise ValueError(
                    f"'{name}' is not a mergeable field; expected one of {MERGEABLE_FIELDS}"
                )
        if "frame" in self.force_merge_fields or "strand" in self.force_merge_fields:
            warnings.warn(
                "Merging features with different strand or frame values may "
                "result in unusable features.",
                UserWarning,
                stacklevel=3,
            )

        if self.force_dialect_check and self.dialect is not None:
            raise ValueError("force_dialect_check is True, but a dialect is provided")
        if self.dialect is not None:
            # Complete it now, so a bad dialect is reported before any work
            # and a partial one (`{"fmt": "gtf"}`) is usable downstream.
            from gffbase.dialect import normalize_dialect

            self.dialect = normalize_dialect(self.dialect)

        if self.checklines < 0:
            raise ValueError(f"checklines must be >= 0; got {self.checklines}")

        # Deprecated since gffutils 0.8.4: a single switch that sets both of
        # the specific ones.
        if not self.infer_gene_extent:
            warnings.warn(
                "infer_gene_extent is deprecated; use disable_infer_genes and "
                "disable_infer_transcripts instead.",
                FutureWarning,
                stacklevel=3,
            )
            self.disable_infer_genes = True
            self.disable_infer_transcripts = True

    @property
    def fuses_multipart(self) -> bool:
        """Whether duplicate ids may become one discontinuous feature.

        Strict mode only, deliberately. gffutils' `merge_strategy="merge"`
        requires all eight non-attribute columns to match, so it never merges a
        genuine discontinuous feature -- it routes them to `create_unique`.
        Fusing is therefore NEW behaviour, not a change to gffutils behaviour,
        and compat mode must not do it.
        """
        return self.mode == MODE_STRICT

    def id_spec_for(self, fmt: str):
        """The effective id_spec, filling in gffutils' per-dialect default."""
        if self.id_spec is not None:
            return self.id_spec
        return DEFAULT_ID_SPEC_GTF if fmt == "gtf" else DEFAULT_ID_SPEC_GFF3

    def resolver_for(self, fmt: str) -> IdSpecResolver:
        return IdSpecResolver(self.id_spec_for(fmt))

    def synthesized_ids_need_resolving(self, fmt: str) -> bool:
        """Whether GTF-synthesized rows must have `id_spec` applied to them.

        The synthesis SQL names each inferred feature after the attribute it
        grouped on -- `gene_id` for a gene, `transcript_id` for a transcript --
        which is exactly what the default spec asks for. So for the default,
        and for any spec that names those same attributes, the SQL answer is
        already the right one and the resolve pass can be skipped entirely.
        Measured identical to gffutils on all six GTF corpora.

        It is only a caller who asks for something else -- `gene_name`, a
        callable -- who needs the slower path.
        """
        if fmt != "gtf":
            return False
        spec = self.id_spec_for(fmt)
        if not isinstance(spec, dict):
            return True
        return any(
            spec.get(featuretype) != attribute
            for featuretype, attribute in self.gtf_group_keys().items()
        )

    def gtf_group_keys(self) -> dict[str, str]:
        """The attribute each inferred GTF parent is grouped on, by type.

        A synthesized row is named after the value it was grouped on, so it
        already satisfies an `id_spec` that names the same attribute.
        """
        return {"gene": self.gtf_gene_key, "transcript": self.gtf_transcript_key}
