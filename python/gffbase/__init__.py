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
"""gffbase — modernized successor to gffutils.

The full drop-in public API surface -- FeatureDB, Feature, create_db,
DataIterator, GFFWriter and the exception hierarchy -- on top of a DuckDB
ingestion engine fed by a Rust parser.
"""

from gffbase.exceptions import (
    AttributeStringError,
    ClosedDatabaseError,
    DuplicateIDError,
    EmptyInputError,
    FeatureNotFoundError,
    MultipartConstraintError,
    ReadOnlyError,
    SchemaVersionError,
)
from gffbase.exceptions import (
    GFFFormatError as _PyGFFFormatError,
)

# Prefer the Rust-defined exception class when the extension
# is loaded — that's the type Rust will actually raise. Fall back to
# the pure-Python definition otherwise. Both inherit from `ValueError`
# so legacy `pytest.raises(ValueError)` callers keep working.
try:  # pragma: no cover — import-time branch
    from gffbase._native import GFFFormatError
except ImportError:
    GFFFormatError = _PyGFFFormatError
# The compatibility submodules are bound on the package namespace because
# gffutils binds them, and the documented one-line migration is
# `import gffbase as gffutils`. Without these, `gffutils.constants.
# always_return_list = True` -- a documented gffutils idiom -- raises
# AttributeError on a package that advertises itself as a drop-in.
#
# Cheap: every one of these is a small pure-Python module with no heavy
# imports, and `interface` already pulls most of them in transitively.
from gffbase import (
    attributes,
    bins,
    constants,
    create,
    ingest,
    merge_criteria,
)
from gffbase.create_db import create_db
from gffbase.feature import Feature, FeatureSegment, MultipartFeature, ParsedFeature
from gffbase.gffwriter import GFFWriter
from gffbase.helpers import example_filename
from gffbase.interface import FeatureDB
from gffbase.iterators import DataIterator
from gffbase.migrate import coalesce_multipart, migrate_v1_to_v2
from gffbase.parser import detect_dialect, native_available, parse_bytes, parse_gff
from gffbase.sqlite_export import export_sqlite
from gffbase.validate import ValidationError, validate_db

__all__ = [
    # Drop-in legacy surface
    "create_db",
    "FeatureDB",
    "Feature",
    "FeatureSegment",
    "MultipartFeature",
    "DataIterator",
    "GFFWriter",
    "example_filename",
    "FeatureNotFoundError",
    "DuplicateIDError",
    "AttributeStringError",
    "EmptyInputError",
    "GFFFormatError",
    "SchemaVersionError",
    "MultipartConstraintError",
    "ReadOnlyError",
    "ClosedDatabaseError",
    "ValidationError",
    "validate_db",
    "migrate_v1_to_v2",
    "coalesce_multipart",
    "merge_criteria",
    # gffbase extras
    "ParsedFeature",
    "parse_gff",
    "parse_bytes",
    "detect_dialect",
    "native_available",
    "ingest",
    "export_sqlite",
    # Compatibility submodules, bound so `import gffbase as gffutils` gives
    # the same attribute access the oracle does.
    "attributes",
    "bins",
    "constants",
    "create",
    "version",
    "__version__",
]

__version__ = "0.2.0"

# Imported LAST, and deliberately: `gffbase.version` re-exports `__version__`
# from this module, so importing it any earlier is a circular import against a
# partially-initialized package.
from gffbase import version  # noqa: E402  (must follow __version__)
