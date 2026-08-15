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
"""Schema and dialect constants, plus the two documented global toggles.

Two of these names are **read at runtime by live code**, not just exported for
completeness:

* `always_return_list` decides whether `feature.attributes["ID"]` gives you
  `["x"]` or `"x"`;
* `ignore_url_escape_characters` turns percent-decoding and re-encoding off.

Both are module-level mutables that callers flip, which is a poor interface but
a documented one — upstream's own doctests set them mid-test. They are read on
each access rather than captured, so a caller's change takes effect
immediately.

The column lists mirror gffutils' **SQLite** schema, which is what
`export_sqlite` writes and what a legacy consumer reads. They are deliberately
not gffbase's own DuckDB schema; that lives in `gffbase.schema` and has more
columns.
"""

from __future__ import annotations

#: gffutils' SQLite schema, verbatim, as `export_sqlite` writes it.
SCHEMA = """
CREATE TABLE features (
    id text,
    seqid text,
    source text,
    featuretype text,
    start int,
    end int,
    score text,
    strand text,
    frame text,
    attributes text,
    extra text,
    bin int,
    primary key (id)
);

CREATE TABLE relations (
    parent text,
    child text,
    level int,
    primary key (parent, child, level)
);

CREATE TABLE meta (
    dialect text,
    version text
);

CREATE TABLE directives (
    directive text
);

CREATE TABLE autoincrements (
    base text,
    n int,
    primary key (base)
);

CREATE TABLE duplicates (
    idspecid text,
    newid text,
    primary key (newid)
);
"""

#: SQLite pragmas gffutils sets by default. **None of these exist in DuckDB**,
#: so `FeatureDB.set_pragmas` skips every one of them. Kept so that a ported
#: script passing `pragmas=constants.default_pragmas` behaves identically.
default_pragmas = {
    "synchronous": "NORMAL",
    "journal_mode": "MEMORY",
    "main.page_size": 4096,
    "main.cache_size": 10000,
}

#: Columns of the legacy `features` table, in order.
_keys = [
    "id",
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "attributes",
    "extra",
    "bin",
]

#: The nine GFF columns.
_gffkeys = [
    "seqid",
    "source",
    "featuretype",
    "start",
    "end",
    "score",
    "strand",
    "frame",
    "attributes",
]

#: The nine, plus whatever followed column 9.
_gffkeys_extra = _gffkeys + ["extra"]

#: Keyword arguments `DataIterator` consumes rather than passing on.
_iterator_kwargs = (
    "data",
    "checklines",
    "transform",
    "force_dialect_check",
    "dialect",
    "from_string",
)

#: Indexes gffutils creates on its SQLite schema. Empty upstream -- the comment
#: there reads "TODO: create indexes once profiling figures out which ones work
#: best". gffbase's own indexes are in `gffbase.schema` and are not optional.
INDEXES: list[str] = []

#: The default GFF3 dialect: what an attribute string is assumed to look like
#: before anything has been sampled.
dialect = {
    "leading semicolon": False,
    "trailing semicolon": False,
    "quoted GFF2 values": False,
    "field separator": ";",
    "semicolon in quotes": False,
    "keyval separator": "=",
    "multival separator": ",",
    "fmt": "gff3",
    "repeated keys": False,
    "order": ["ID", "Name", "gene_id", "transcript_id"],
}

#: When True (the default), `feature.attributes[key]` always returns a list,
#: even for a single value. Set False for the older scalar-unwrapping
#: behaviour. Read live on every access.
always_return_list = True

#: When True, percent-escapes in attribute values are left exactly as they
#: appear -- neither decoded on read nor re-encoded on write. Default False.
ignore_url_escape_characters = False
