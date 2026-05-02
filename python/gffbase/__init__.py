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
)
from gffbase.feature import Feature, ParsedFeature
from gffbase.parser import parse_gff, parse_bytes, detect_dialect, native_available
from gffbase import ingest
from gffbase import merge_criteria
from gffbase.helpers import example_filename
from gffbase.gffwriter import GFFWriter
from gffbase.iterators import DataIterator
from gffbase.interface import FeatureDB
from gffbase.create_db import create_db
from gffbase.sqlite_export import export_sqlite

__all__ = [
    # Drop-in legacy surface
    "create_db",
    "FeatureDB",
    "Feature",
    "DataIterator",
    "GFFWriter",
    "example_filename",
    "FeatureNotFoundError",
    "DuplicateIDError",
    "AttributeStringError",
    "EmptyInputError",
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

__version__ = "0.0.1"
