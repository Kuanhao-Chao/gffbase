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

Phase 5: full drop-in public API surface (FeatureDB, Feature, create_db,
DataIterator, GFFWriter, exceptions) on top of the Phase 4 DuckDB ingestion
engine.
"""

from gffbase.exceptions import (
    AttributeStringError,
    DuplicateIDError,
    EmptyInputError,
    FeatureNotFoundError,
    MultipartConstraintError,
    SchemaVersionError,
)
from gffbase.exceptions import (
    GFFFormatError as _PyGFFFormatError,
)

# Phase 16: prefer the Rust-defined exception class when the extension
# is loaded — that's the type Rust will actually raise. Fall back to
# the pure-Python definition otherwise. Both inherit from `ValueError`
# so legacy `pytest.raises(ValueError)` callers keep working.
try:  # pragma: no cover — import-time branch
    from gffbase._native import GFFFormatError
except ImportError:
    GFFFormatError = _PyGFFFormatError
from gffbase import ingest, merge_criteria
from gffbase.create_db import create_db
from gffbase.feature import Feature, FeatureSegment, MultipartFeature, ParsedFeature
from gffbase.gffwriter import GFFWriter
from gffbase.helpers import example_filename
from gffbase.interface import FeatureDB
from gffbase.iterators import DataIterator
from gffbase.parser import detect_dialect, native_available, parse_bytes, parse_gff
from gffbase.sqlite_export import export_sqlite

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
    "merge_criteria",
    # gffbase extras
    "ParsedFeature",
    "parse_gff",
    "parse_bytes",
    "detect_dialect",
    "native_available",
    "ingest",
    "export_sqlite",
    "__version__",
]

__version__ = "0.1.1"
