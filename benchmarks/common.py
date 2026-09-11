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
"""Shared benchmarking utilities.

* `environment()` — full provenance for a run: hardware, OS, every version,
  git commit. Stamped into every results file.
* `run_subprocess` — Popen + RSS sampler, returns merged dict.
* `merge_results` — update a results file BY KEY, preserving the rest.
* `repeat` — run a measurement N times, report median and spread.
* `purge_db` / `require_free_disk` — the multi-GB corpora do not all fit.
* `du(path)` — recursive on-disk size.
* `pretty_*` formatters.

Nothing here loads a cached measurement. A previous version of this module
scraped timings out of a sibling `bench/out/` directory and presented them as
current results, which is how the published table came to mix numbers from two
different GENCODE releases.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import lzma
import math
import os
import platform
import re
import shutil
import stat
import statistics
import struct
import subprocess
import sys
import tempfile
import time
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from email.parser import BytesParser
from pathlib import Path
from typing import Any

# `psutil` is imported lazily, inside the two functions that measure with it.
# At module scope it made `06_mega.py --help` -- and every import of this
# module -- fail without the `bench` extra, which took out 15 of 18 CI jobs.
# A `--help` that dies on a missing measurement library is a bug of its own,
# independent of the tests that caught it.

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
DATA = BENCH_DIR / "data"
# Overridable so a run can target an external volume. A full sweep needs
# ~14 GiB transient, which is more than a full laptop disk has spare.
OUT = Path(os.environ.get("GFFBASE_BENCH_OUT") or (BENCH_DIR / "out"))
#: Committed results. `out/` is scratch and gitignored; this is the record.
RESULTS = BENCH_DIR / "results"

#: The corpus stages 01-05 measure. GFF3 rather than GTF because legacy
#: gffutils has to FINISH for those comparisons to mean anything, and legacy
#: GTF ingest on v49 does not finish inside any reasonable cap. v49 rather
#: than v45 because v45 is what `06_mega.py` stopped using, and having the two
#: harnesses read different releases is what let a v45 spatial number be
#: published in a row labelled v49.
GENCODE_GFF3 = DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz"
GENCODE_GTF = DATA / "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz"
GFFBASE_DB = OUT / "gencode-gff3.duckdb"
LEGACY_DB = OUT / "gencode-gff3_legacy.sqlite"

#: Results schema. Bump when the shape changes so a stale file is detectable
#: rather than silently misread.
SCHEMA_VERSION = "3"
_CANDIDATE_VERSION = "0.2.0rc1"
_COMPARATOR_VERSION = "0.14"
_MAX_CANDIDATE_WHEEL_BYTES = 256 << 20
_MAX_CANDIDATE_WHEEL_MEMBERS = 4096
_MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES = 512 << 20
_MAX_CANDIDATE_NATIVE_BYTES = 128 << 20
_MAX_CANDIDATE_METADATA_BYTES = 1 << 20
_ZIP_EOCD_SIZE = 22
_ZIP64_LOCATOR_SIZE = 20
_ZIP64_EOCD_MIN_SIZE = 56
_ZIP_CENTRAL_HEADER_SIZE = 46

# These are applied to every ingest child.  Keeping the complete set in the
# top-level provenance lets a controller reject a run that would otherwise
# look comparable while silently oversubscribing a helper library.
_BENCHMARK_ENV_KEYS = (
    "GFFBASE_THREADS",
    "GFFUTILS2_THREADS",
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "BLIS_NUM_THREADS",
    "DUCKDB_DISABLE_PROGRESS_BAR",
    "PYTHONUNBUFFERED",
)

FULL_VALIDATION_IDS = (
    "INV-1",
    "INV-2",
    "INV-3",
    "INV-4",
    "INV-5",
    "INV-6",
    "INV-7",
    "INV-9",
    "INV-10",
    "INV-11",
    "INV-11b",
    "INV-13",
    "INV-14",
    "INV-15",
    "INV-15a",
    "INV-16",
    "INV-11-exact",
    "INV-12",
)

_PRIMARY_CORPORA = {
    "mane": {
        "name": "MANE v1.5 (Ensembl IDs)",
        "filename": "MANE.GRCh38.v1.5.ensembl_genomic.gff.gz",
        "fmt": "gff3",
        "bytes": 10_349_746,
        "sha256": "69089bbc84d1d3c3ce31c2ed3f85b6c3169fb8836d092a082623c59a43fd22ef",
    },
    "chess": {
        "name": "CHESS 3.1.3",
        "filename": "chess3.1.3.GRCh38.gff.gz",
        "fmt": "gff3",
        "bytes": 20_435_645,
        "sha256": "28da847be976780fe38162a7c244749fdc7a0b446741ca8da2b64019c1606e03",
    },
    "refseq": {
        "name": "RefSeq GRCh38.p14",
        "filename": "GCF_000001405.40_GRCh38.p14_genomic.gff.gz",
        "fmt": "gff3",
        "bytes": 78_190_483,
        "sha256": "4920f0eae7e2197c50b67a201e06d657387137b49dd60f474b4f1d5b29334051",
    },
    "gencode-gtf": {
        "name": "GENCODE v49 (GTF)",
        "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gtf.gz",
        "fmt": "gtf",
        "bytes": 70_588_995,
        "sha256": "576dddae36169ad648afbe706535361309786e549ad7daf529cca7674fb0058f",
    },
    "gencode-gff3": {
        "name": "GENCODE v49 (GFF3)",
        "filename": "gencode.v49.chr_patch_hapl_scaff.basic.annotation.gff3.gz",
        "fmt": "gff3",
        "bytes": 89_385_177,
        "sha256": "22ffa691aac993603f7f21effacf19848262bec74545bd979863e7af602e5a1d",
    },
}


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def _cmd(*args: str) -> str | None:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def _pkg_version(name: str) -> str | None:
    try:
        import importlib.metadata as md

        return md.version(name)
    except Exception:
        return None


def _cpu_model() -> str | None:
    if sys.platform == "darwin":
        return _cmd("sysctl", "-n", "machdep.cpu.brand_string")
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _runtime_libc_identity() -> dict[str, str | None]:
    """Return a closed, portable libc identity from standard-library evidence."""

    if not sys.platform.startswith("linux"):
        return {"family": None, "version": None}
    raw_family, raw_version = platform.libc_ver()
    family = {
        "glibc": "glibc",
        "gnu libc": "glibc",
        "musl": "musl",
    }.get(raw_family.casefold(), "unknown")
    if family == "unknown" or not re.fullmatch(r"\d+(?:\.\d+)+", raw_version):
        return {"family": family, "version": None}
    version = ".".join(str(int(component)) for component in raw_version.split("."))
    return {"family": family, "version": version}


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash *path* without loading a benchmark corpus into memory."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_corpus(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    check_gzip: bool = True,
) -> dict:
    """Verify a corpus against its immutable registry entry.

    Reading a gzip stream to EOF also verifies its trailer/CRC. The returned
    path is run-local diagnostic metadata, not portable provenance.
    """

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    size = path.stat().st_size
    if size != expected_size:
        raise ValueError(f"{path.name}: expected {expected_size} bytes, found {size}")
    digest = sha256_file(path)
    if digest != expected_sha256:
        raise ValueError(f"{path.name}: expected sha256 {expected_sha256}, found {digest}")
    gzip_crc_ok = None
    if check_gzip and (path.suffix == ".gz" or path.name.endswith(".gz.part")):
        with gzip.open(path, "rb") as handle:
            while handle.read(1 << 20):
                pass
        gzip_crc_ok = True
    return {
        "path": str(path),
        "bytes": size,
        "sha256": digest,
        "gzip_crc_ok": gzip_crc_ok,
    }


def benchmark_env(threads: int) -> dict[str, str]:
    """Environment for one benchmark process with bounded helper pools."""

    if threads < 1:
        raise ValueError("threads must be >= 1")
    return {
        "GFFBASE_THREADS": str(threads),
        # gffbase 0.1.0 used this historical name while constructing the
        # database.  Keeping both names is harmless for current releases and
        # is required for a bounded bridge comparison.
        "GFFUTILS2_THREADS": str(threads),
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
        "BLIS_NUM_THREADS": "1",
        "DUCKDB_DISABLE_PROGRESS_BAR": "1",
        "PYTHONUNBUFFERED": "1",
    }


def configure_duckdb_connection(con, threads: int) -> None:
    """Apply the campaign's thread limit to a DuckDB connection."""

    if threads < 1:
        raise ValueError("threads must be >= 1")
    con.execute(f"PRAGMA threads = {int(threads)}")


def _installed_gffbase() -> dict:
    """Best-effort identity of the Python package and native extension."""

    info: dict = {"distribution_version": _pkg_version("gffbase")}
    try:
        import gffbase

        info.update(
            {
                "python_version": getattr(gffbase, "__version__", None),
                # Absolute module paths identify the runner, not the artifact.
                "python_module": Path(gffbase.__file__).name,
            }
        )
        try:
            from gffbase import _native

            native_path = Path(_native.__file__).resolve()
            info.update(
                {
                    "native_version": getattr(_native, "__version__", None),
                    "native_module": native_path.name,
                    "native_sha256": sha256_file(native_path),
                }
            )
        except Exception as exc:  # pure-Python installs remain inspectable
            info["native_error"] = type(exc).__name__
    except Exception as exc:
        info["import_error"] = type(exc).__name__
    return info


def _zip_directory_metadata(path: Path) -> tuple[int, int, int]:
    """Return ``(entry_count, central_offset, central_size)`` without ``ZipFile``.

    ``zipfile.ZipFile`` materializes the complete central directory in memory.
    Read the bounded end records first so an archive claiming an excessive
    number of members is rejected before that allocation. Wheels are required
    to be single-disk ZIP files without trailing bytes.
    """

    path = Path(path)
    try:
        size = path.stat().st_size
        if size < _ZIP_EOCD_SIZE:
            raise ValueError("candidate wheel central directory is missing")
        tail_size = min(size, _ZIP_EOCD_SIZE + 0xFFFF + _ZIP64_LOCATOR_SIZE)
        with path.open("rb") as handle:
            tail_offset = size - tail_size
            handle.seek(tail_offset)
            tail = handle.read(tail_size)

            relative = tail.rfind(b"PK\x05\x06")
            while relative >= 0:
                if relative + _ZIP_EOCD_SIZE <= len(tail):
                    comment_size = struct.unpack_from("<H", tail, relative + 20)[0]
                    absolute = tail_offset + relative
                    if absolute + _ZIP_EOCD_SIZE + comment_size == size:
                        break
                relative = tail.rfind(b"PK\x05\x06", 0, relative)
            if relative < 0:
                raise ValueError("candidate wheel central directory is missing")

            eocd_offset = tail_offset + relative
            (
                signature,
                disk_number,
                central_disk,
                entries_on_disk,
                entry_count,
                central_size,
                central_offset,
                _comment_size,
            ) = struct.unpack_from("<4s4H2IH", tail, relative)
            if signature != b"PK\x05\x06" or disk_number != 0 or central_disk != 0:
                raise ValueError("candidate wheel central directory is not single-disk")

            uses_zip64 = (
                entries_on_disk == 0xFFFF
                or entry_count == 0xFFFF
                or central_size == 0xFFFFFFFF
                or central_offset == 0xFFFFFFFF
            )
            if not uses_zip64:
                if entries_on_disk != entry_count:
                    raise ValueError("candidate wheel central directory counts disagree")
                if central_offset + central_size != eocd_offset:
                    raise ValueError("candidate wheel central directory bounds are invalid")
                return int(entry_count), int(central_offset), int(central_size)

            locator_offset = eocd_offset - _ZIP64_LOCATOR_SIZE
            if locator_offset < 0:
                raise ValueError("candidate wheel ZIP64 central directory is missing")
            handle.seek(locator_offset)
            locator = handle.read(_ZIP64_LOCATOR_SIZE)
            if len(locator) != _ZIP64_LOCATOR_SIZE:
                raise ValueError("candidate wheel ZIP64 central directory is truncated")
            locator_signature, zip64_disk, zip64_offset, total_disks = struct.unpack(
                "<4sIQI", locator
            )
            if (
                locator_signature != b"PK\x06\x07"
                or zip64_disk != 0
                or total_disks != 1
                or zip64_offset + _ZIP64_EOCD_MIN_SIZE > locator_offset
            ):
                raise ValueError("candidate wheel ZIP64 central directory is invalid")

            handle.seek(zip64_offset)
            zip64_record = handle.read(_ZIP64_EOCD_MIN_SIZE)
            if len(zip64_record) != _ZIP64_EOCD_MIN_SIZE:
                raise ValueError("candidate wheel ZIP64 central directory is truncated")
            (
                zip64_signature,
                record_size,
                _made_by,
                _needed,
                zip64_disk_number,
                zip64_central_disk,
                zip64_entries_on_disk,
                zip64_entry_count,
                zip64_central_size,
                zip64_central_offset,
            ) = struct.unpack("<4sQ2H2I4Q", zip64_record)
            if (
                zip64_signature != b"PK\x06\x06"
                or record_size < 44
                or zip64_offset + 12 + record_size != locator_offset
                or zip64_disk_number != 0
                or zip64_central_disk != 0
                or zip64_entries_on_disk != zip64_entry_count
                or zip64_central_offset + zip64_central_size != zip64_offset
                or (entries_on_disk != 0xFFFF and entries_on_disk != zip64_entries_on_disk)
                or (entry_count != 0xFFFF and entry_count != zip64_entry_count)
                or (central_size != 0xFFFFFFFF and central_size != zip64_central_size)
                or (central_offset != 0xFFFFFFFF and central_offset != zip64_central_offset)
            ):
                raise ValueError("candidate wheel ZIP64 central directory is invalid")
            return (
                int(zip64_entry_count),
                int(zip64_central_offset),
                int(zip64_central_size),
            )
    except OSError as exc:
        raise ValueError("candidate wheel central directory could not be read") from exc


def _zip_entry_count_before_open(path: Path) -> int:
    """Return the declared ZIP entry count before ``ZipFile`` materializes it."""

    return _zip_directory_metadata(Path(path))[0]


def _raw_central_directory_names(
    path: Path, *, entry_count: int, central_offset: int, central_size: int
) -> list[str]:
    """Decode member names directly so embedded NUL bytes cannot be hidden."""

    names: list[str] = []
    try:
        with Path(path).open("rb") as handle:
            handle.seek(central_offset)
            central_end = central_offset + central_size
            for _ in range(entry_count):
                header = handle.read(_ZIP_CENTRAL_HEADER_SIZE)
                if len(header) != _ZIP_CENTRAL_HEADER_SIZE:
                    raise ValueError("candidate wheel central directory is truncated")
                fields = struct.unpack("<4s6H3I5H2I", header)
                if fields[0] != b"PK\x01\x02":
                    raise ValueError("candidate wheel central directory entry is invalid")
                flags = fields[3]
                name_size, extra_size, comment_size = fields[10:13]
                raw_name = handle.read(name_size)
                if len(raw_name) != name_size:
                    raise ValueError("candidate wheel central directory name is truncated")
                if b"\x00" in raw_name:
                    raise ValueError("candidate wheel member name contains NUL")
                encoding = "utf-8" if flags & 0x800 else "cp437"
                try:
                    name = raw_name.decode(encoding)
                except UnicodeDecodeError as exc:
                    raise ValueError("candidate wheel member path is not decodable") from exc
                names.append(name)
                handle.seek(extra_size + comment_size, os.SEEK_CUR)
                if handle.tell() > central_end:
                    raise ValueError("candidate wheel central directory bounds are invalid")
            if handle.tell() != central_end:
                raise ValueError(
                    "candidate wheel central directory count differs from its contents"
                )
    except OSError as exc:
        raise ValueError("candidate wheel central directory could not be read") from exc
    return names


def _validate_wheel_member_names(names: list[str]) -> None:
    """Reject non-portable names and aliases before reading archive members."""

    canonical_keys: set[str] = set()
    for name in names:
        key = unicodedata.normalize("NFC", name).casefold()
        if key in canonical_keys:
            raise ValueError("candidate wheel contains ambiguous member paths")
        canonical_keys.add(key)

    namespace: dict[str, bool] = {}
    for name in names:
        normalized = unicodedata.normalize("NFC", name)
        is_directory = name.endswith("/")
        body = name[:-1] if is_directory else name
        parts = body.split("/")
        if (
            not body
            or name != normalized
            or name.startswith("/")
            or "\\" in name
            or re.match(r"^[A-Za-z]:", name)
            or any(part in {"", ".", ".."} for part in parts)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise ValueError("candidate wheel member path is not canonical and portable")
        namespace_key = body.casefold()
        if namespace_key in namespace:
            raise ValueError("candidate wheel contains an ambiguous file/directory namespace")
        namespace[namespace_key] = is_directory

    regular_files = {key for key, is_directory in namespace.items() if not is_directory}
    for key in namespace:
        parts = key.split("/")
        if any("/".join(parts[:index]) in regular_files for index in range(1, len(parts))):
            raise ValueError("candidate wheel contains a file/directory namespace conflict")


def _wheel_member_type_is_safe(member: zipfile.ZipInfo) -> bool:
    """Return whether a member is a regular file/directory or has no POSIX type."""

    mode = (member.external_attr >> 16) & 0xFFFF if member.create_system == 3 else 0
    file_type = stat.S_IFMT(mode)
    if member.is_dir():
        return file_type in {0, stat.S_IFDIR}
    return file_type in {0, stat.S_IFREG}


def _candidate_wheel_artifact(install: dict) -> dict:
    """Inspect and bind the configured candidate wheel to the installed native."""

    configured_path = os.environ.get("GFFBASE_BENCH_WHEEL")
    configured_sha256 = os.environ.get("GFFBASE_BENCH_WHEEL_SHA256")
    if configured_path is None and configured_sha256 is None:
        return {"wheel": None, "wheel_sha256": None}
    if configured_path is None or not isinstance(configured_sha256, str):
        raise ValueError("candidate wheel SHA-256 and path must both be configured")
    if not re.fullmatch(r"[0-9a-f]{64}", configured_sha256):
        raise ValueError("candidate wheel SHA-256 is invalid")

    wheel_path = Path(configured_path)
    if not wheel_path.is_file():
        raise ValueError("candidate wheel path is not a file")
    wheel_name = wheel_path.name
    filename_tags = _candidate_wheel_tags(wheel_name)
    if filename_tags is None:
        raise ValueError("candidate wheel filename is invalid")

    with tempfile.TemporaryDirectory(prefix=".gffbase-wheel-") as directory:
        snapshot = Path(directory) / wheel_name
        digest = hashlib.sha256()
        copied = 0
        try:
            with wheel_path.open("rb") as source, snapshot.open("wb") as target:
                while chunk := source.read(1 << 20):
                    copied += len(chunk)
                    if copied > _MAX_CANDIDATE_WHEEL_BYTES:
                        raise ValueError("candidate wheel exceeds the compressed size limit")
                    digest.update(chunk)
                    target.write(chunk)
        except OSError as exc:
            raise ValueError("candidate wheel could not be read") from exc
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != configured_sha256:
            raise ValueError("candidate wheel SHA-256 differs from the configured digest")

        entry_count, central_offset, central_size = _zip_directory_metadata(snapshot)
        if entry_count > _MAX_CANDIDATE_WHEEL_MEMBERS:
            raise ValueError("candidate wheel exceeds the member-count limit")
        raw_names = _raw_central_directory_names(
            snapshot,
            entry_count=entry_count,
            central_offset=central_offset,
            central_size=central_size,
        )
        _validate_wheel_member_names(raw_names)

        try:
            with zipfile.ZipFile(snapshot) as archive:
                members = archive.infolist()
                names = [member.filename for member in members]
                if len(members) != entry_count or names != raw_names:
                    raise ValueError("candidate wheel central directory decoding is ambiguous")
                if any(not _wheel_member_type_is_safe(member) for member in members):
                    raise ValueError("candidate wheel members must be regular files or directories")
                if any(member.flag_bits & 0x1 for member in members):
                    raise ValueError("candidate wheel must not contain encrypted members")
                if sum(member.file_size for member in members) > (
                    _MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES
                ):
                    raise ValueError("candidate wheel exceeds the uncompressed size limit")
                expected_dist_info = f"gffbase-{_CANDIDATE_VERSION}.dist-info"
                dist_info_roots = {
                    name.split("/", 1)[0]
                    for name in names
                    if name.split("/", 1)[0].casefold().endswith(".dist-info")
                }
                if dist_info_roots != {expected_dist_info}:
                    raise ValueError("candidate wheel dist-info tree is invalid")
                metadata_members = [
                    member for member in members if member.filename.endswith(".dist-info/METADATA")
                ]
                wheel_members = [
                    member for member in members if member.filename.endswith(".dist-info/WHEEL")
                ]
                native_members = [
                    member
                    for member in members
                    if member.filename.count("/") == 1
                    and member.filename.startswith("gffbase/")
                    and member.filename.rsplit("/", 1)[-1].startswith("_native.")
                    and member.filename.endswith((".so", ".pyd"))
                ]
                if len(metadata_members) != 1 or len(wheel_members) != 1:
                    raise ValueError("candidate wheel metadata members are not unique")
                if (
                    metadata_members[0].filename != f"{expected_dist_info}/METADATA"
                    or wheel_members[0].filename != f"{expected_dist_info}/WHEEL"
                ):
                    raise ValueError("candidate wheel dist-info directory is invalid")
                if len(native_members) != 1:
                    raise ValueError("candidate wheel must contain exactly one native member")
                if any(
                    member.file_size > _MAX_CANDIDATE_METADATA_BYTES
                    for member in (metadata_members[0], wheel_members[0])
                ):
                    raise ValueError("candidate wheel metadata exceeds the size limit")
                native_info = native_members[0]
                if native_info.file_size > _MAX_CANDIDATE_NATIVE_BYTES:
                    raise ValueError("candidate wheel native member exceeds the size limit")
                native_member = native_info.filename
                native_digest = hashlib.sha256()
                metadata_bytes = bytearray()
                wheel_bytes = bytearray()
                actual_total = 0
                for member in members:
                    member_bytes = 0
                    with archive.open(member) as member_handle:
                        while chunk := member_handle.read(1 << 20):
                            member_bytes += len(chunk)
                            actual_total += len(chunk)
                            if actual_total > _MAX_CANDIDATE_WHEEL_UNCOMPRESSED_BYTES:
                                raise ValueError(
                                    "candidate wheel exceeds the uncompressed size limit"
                                )
                            if member is metadata_members[0]:
                                if member_bytes > _MAX_CANDIDATE_METADATA_BYTES:
                                    raise ValueError(
                                        "candidate wheel metadata exceeds the size limit"
                                    )
                                metadata_bytes.extend(chunk)
                            elif member is wheel_members[0]:
                                if member_bytes > _MAX_CANDIDATE_METADATA_BYTES:
                                    raise ValueError(
                                        "candidate wheel metadata exceeds the size limit"
                                    )
                                wheel_bytes.extend(chunk)
                            elif member is native_info:
                                if member_bytes > _MAX_CANDIDATE_NATIVE_BYTES:
                                    raise ValueError(
                                        "candidate wheel native member exceeds the size limit"
                                    )
                                native_digest.update(chunk)
                    if member_bytes != member.file_size:
                        raise ValueError("candidate wheel member size differs from its header")
                    if member in (metadata_members[0], wheel_members[0]) and (
                        member_bytes > _MAX_CANDIDATE_METADATA_BYTES
                    ):
                        raise ValueError("candidate wheel metadata exceeds the size limit")
                    if member is native_info and member_bytes > _MAX_CANDIDATE_NATIVE_BYTES:
                        raise ValueError("candidate wheel native member exceeds the size limit")
                metadata = BytesParser().parsebytes(bytes(metadata_bytes))
                wheel_metadata = BytesParser().parsebytes(bytes(wheel_bytes))
                native_sha256 = native_digest.hexdigest()
        except (
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            NotImplementedError,
            RuntimeError,
            EOFError,
            OSError,
            lzma.LZMAError,
            zlib.error,
        ) as exc:
            raise ValueError("candidate wheel is not a valid ZIP archive") from exc

    metadata_names = metadata.get_all("Name") or []
    metadata_versions = metadata.get_all("Version") or []
    if metadata_names != ["gffbase"] or metadata_versions != [_CANDIDATE_VERSION]:
        raise ValueError("candidate wheel metadata identity is invalid")
    metadata_name = metadata_names[0]
    metadata_version = metadata_versions[0]
    wheel_tags = wheel_metadata.get_all("Tag") or []
    expected_tag = "-".join(filename_tags)
    if wheel_tags != [expected_tag]:
        raise ValueError("candidate WHEEL Tag differs from its filename")
    native_name = native_member.rsplit("/", 1)[-1]
    if native_name != install.get("native_module") or native_sha256 != install.get("native_sha256"):
        raise ValueError("candidate wheel does not contain the installed native binary")
    if any(
        install.get(key) != _CANDIDATE_VERSION
        for key in ("distribution_version", "python_version", "native_version")
    ):
        raise ValueError("installed native identity differs from candidate metadata")
    return {
        "wheel": wheel_name,
        "wheel_sha256": actual_sha256,
        "metadata": {"name": metadata_name, "version": metadata_version},
        "wheel_tags": wheel_tags,
        "native": {"member": native_member, "sha256": native_sha256},
    }


def environment(*, benchmark_controls: dict[str, str] | None = None) -> dict:
    """Everything needed to reproduce or fairly compare a run.

    None of this was recorded before: the published numbers carried their
    hardware and versions only as hand-typed prose in a markdown file, which
    said "gffbase 0.1.0" for the entire 0.2.0 development cycle. A benchmark
    result that does not say what it ran on is not a measurement, and one that
    does not say what version it measured cannot detect a regression.
    """
    import psutil  # type: ignore[import-untyped]

    free = shutil.disk_usage(str(OUT if OUT.exists() else BENCH_DIR)).free
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
    if benchmark_controls is None:
        try:
            threads = int(os.environ.get("GFFBASE_THREADS", "1"))
        except ValueError:
            threads = 1
        benchmark_controls = benchmark_env(max(threads, 1))
    if set(benchmark_controls) != set(_BENCHMARK_ENV_KEYS):
        raise ValueError("benchmark_controls must contain the complete bounded environment")
    install = _installed_gffbase()
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _cmd("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "git_dirty": bool(_cmd("git", "-C", str(ROOT), "status", "--porcelain")),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "libc": _runtime_libc_identity(),
        "cpu_model": _cpu_model(),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "total_ram_bytes": psutil.virtual_memory().total,
        "free_disk_bytes": free,
        "cpu_affinity": affinity,
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": Path(sys.executable).name,
        },
        "rustc_version": _cmd("rustc", "--version"),
        "packages": {
            name: _pkg_version(name)
            for name in ("gffbase", "duckdb", "pyarrow", "pandas", "polars", "gffutils", "psutil")
        },
        "gffbase_install": install,
        "artifact": _candidate_wheel_artifact(install),
        # Do not snapshot an arbitrary parent shell.  These are the actual
        # bounded controls applied by ``run_subprocess`` for a harness run.
        "env": dict(benchmark_controls),
    }


# ---------------------------------------------------------------------------
# Disk management
# ---------------------------------------------------------------------------


def require_free_disk(gib: float, *, what: str = "this stage") -> None:
    """Abort before starting work that cannot finish.

    A corpus pair is up to 13 GiB and a partial run leaves it behind, so
    running out of space mid-sweep costs the hours already spent AND blocks
    the retry. Checking first is cheap.
    """
    free = shutil.disk_usage(str(OUT if OUT.exists() else BENCH_DIR)).free
    need = int(gib * (1 << 30))
    if free < need:
        raise SystemExit(
            f"refusing to start {what}: needs ~{gib:.1f} GiB free, "
            f"{free / (1 << 30):.1f} GiB available in {OUT}. "
            "Free space, or set GFFBASE_BENCH_OUT to a larger volume."
        )


def purge_db(*paths: Path) -> int:
    """Delete databases and their sidecars. Returns bytes reclaimed.

    The sidecars matter: DuckDB leaves `.wal`, SQLite leaves `-journal` and
    `-wal`, and unlinking only the bare filename leaks them. A sweep that
    leaks 13 GiB per corpus does not reach the end.
    """
    freed = 0
    for path in paths:
        for candidate in (
            path,
            Path(str(path) + ".wal"),
            Path(str(path) + ".tmp"),
            Path(str(path) + "-journal"),
            Path(str(path) + "-wal"),
            Path(str(path) + "-shm"),
        ):
            try:
                if candidate.is_file():
                    freed += candidate.stat().st_size
                    candidate.unlink()
            except OSError:
                pass
    return freed


# ---------------------------------------------------------------------------
# Repeats
# ---------------------------------------------------------------------------


def repeat(fn, n: int, *, warmup: int = 0) -> dict:
    """Run `fn` n times and summarize.

    For `n == 1` the result deliberately has NO `median` key, so a downstream
    renderer physically cannot print a median for a single sample. Every
    number published before this existed was n=1 presented to three
    significant figures under a claim of being "deterministic to ~10 %".
    """
    for _ in range(warmup):
        fn()
    values = [fn() for _ in range(n)]
    if n == 1:
        return {"value": values[0], "n": 1}
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "values": values,
        "n": n,
    }


# ---------------------------------------------------------------------------
# Process / disk measurement
# ---------------------------------------------------------------------------


def run_subprocess(
    script: str,
    *,
    label: str,
    timeout: int | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict:
    """Run a Python -c snippet in a fresh subprocess. Polls RSS at 50ms.
    Discards stderr (DuckDB progress bars) but captures stdout's last
    JSON line."""
    import psutil

    env = os.environ.copy()
    applied_env = {
        "DUCKDB_DISABLE_PROGRESS_BAR": "1",
        "PYTHONUNBUFFERED": "1",
        **(env_extra or {}),
    }
    env.update(applied_env)
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    p = psutil.Process(proc.pid)
    peak = 0
    deadline = time.time() + timeout if timeout else None
    timed_out = False
    try:
        while proc.poll() is None:
            try:
                rss = p.memory_info().rss
                for c in p.children(recursive=True):
                    try:
                        rss += c.memory_info().rss
                    except psutil.Error:
                        pass
                if rss > peak:
                    peak = rss
            except psutil.Error:
                pass
            if deadline and time.time() > deadline:
                proc.kill()
                timed_out = True
                break
            time.sleep(0.05)
    finally:
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    text = out.decode("utf-8", errors="replace").strip()
    if timed_out:
        state = "timed_out"
    elif proc.returncode == 0:
        state = "completed"
    else:
        state = "failed"
    info: dict = {
        "label": label,
        "peak_rss_bytes": peak,
        "peak_rss_mb": peak / (1024 * 1024),
        "exit_code": proc.returncode,
        "state": state,
        "benchmark_env": applied_env,
    }
    if timeout is not None:
        info["cap_seconds"] = timeout
    last = text.splitlines()[-1] if text else ""
    parse_error = None
    try:
        payload = json.loads(last)
        if not isinstance(payload, dict):
            raise ValueError("last stdout line is not a JSON object")
        reserved = set(info)
        collisions = sorted(reserved.intersection(payload))
        if collisions:
            raise ValueError(f"child payload overwrites authoritative fields: {collisions}")
        if state == "completed":
            info.update(payload)
    except (ValueError, json.JSONDecodeError) as exc:
        parse_error = str(exc)
    if state != "completed" or parse_error is not None:
        # Child stdout can contain local input paths, commands, or exception
        # text.  Keep bounded diagnostic evidence without serializing it.
        info["stdout_bytes"] = len(out)
        info["stdout_sha256"] = hashlib.sha256(out).hexdigest()
        info["stdout_parse_error"] = "invalid final JSON object"
    if state != "completed":
        # A killed or failed process has no completed ingest wall.  Never let
        # a partial child payload turn the cap into a measurement.
        info["wall_seconds"] = None
    return info


def du(path: Path) -> int:
    """Total size in bytes — handles both files and directories."""
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


# ---------------------------------------------------------------------------
# Correctness signatures
# ---------------------------------------------------------------------------


def _hash_cursor(cursor) -> tuple[str, int]:
    """Return a deterministic digest and row count for a sorted SQL cursor."""

    digest = hashlib.sha256()
    count = 0
    while True:
        rows = cursor.fetchmany(10_000)
        if not rows:
            break
        for row in rows:
            encoded = json.dumps(
                list(row), ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    return digest.hexdigest(), count


class _RowsCursor:
    """Small cursor adapter used to hash normalized Python-side rows."""

    def __init__(self, rows):
        self._rows = iter(rows)

    def fetchmany(self, size: int):
        rows = []
        for _ in range(size):
            try:
                rows.append(next(self._rows))
            except StopIteration:
                break
        return rows


#: The GFF3 multi-value separator, and the single tokenization the signature
#: applies to both engines.  Neither engine records the separator it used, so
#: it is spelled once here rather than read back from a dialect.
_MULTIVAL_SEPARATOR = ","

#: A comma followed by a space, which gffutils treats as prose rather than a
#: separator (``gffutils/parser.py``).  The signature adopts that reading for
#: both engines; see `_canonical_attribute_rows` for why the coarser rule wins.
_PROSE_MULTIVAL_SEPARATOR = ", "

#: Ceiling on derived hierarchy depth.  Real annotations are three or four
#: levels; anything beyond this is a cycle, and a cycle would otherwise extend
#: the frontier forever.  Deep enough that no legitimate corpus reaches it.
_MAX_CLOSURE_DEPTH = 64


def _canonical_attribute_rows(values: dict) -> Iterator[tuple[str, int, str | None]]:
    """Yield a deterministic semantic representation of column nine.

    GFF/GTF attribute key order is not semantic and the two engines expose it
    differently for inferred parents.  Keys are therefore sorted, while the
    order of values for each key is retained.  A key with no values is emitted
    with index ``-1`` and ``None`` so flags such as ``pseudo`` remain covered.

    Each key's values are rejoined and re-split under ONE rule, because the two
    engines do not agree on where a value ends.  GFF3 says an unescaped comma
    separates values and a literal comma must be percent-encoded, so gffbase
    splits on every unescaped comma; gffutils reads a comma *followed by a
    space* as prose and keeps it inside the value, deliberately, so that an
    unescaped ``description=kinase, subunit 1`` survives.  Ten CHESS gene
    records carry two names that way (``gene_name=ADAM6, RPS8P1``), which
    reported the corpus as a 20-row content divergence and cost it its
    published speedup.  That is a parse policy -- declared in the parity
    register -- not a difference in what was ingested.

    The rule applied is gffutils', not gffbase's, because it is the coarser of
    the two and a comparison cannot be finer than its least precise side.
    gffbase keeps ``description=killer cell receptor%2C three Ig domains`` as
    one value, correctly, since the comma is escaped; gffutils decodes the
    escape first and can no longer tell that value from two.  Re-splitting on
    every comma would therefore shatter 2,294 correctly escaped CHESS
    descriptions to make 20 gene names line up.  Folding to the coarser rule
    leaves both untouched and costs only the ability to distinguish an escaped
    comma from a separator -- which gffutils had already lost.
    """

    for key in sorted(values):
        raw_values = values[key]
        items = raw_values if isinstance(raw_values, (list, tuple)) else [raw_values]
        if not items:
            yield str(key), -1, None
            continue
        rejoined = _MULTIVAL_SEPARATOR.join(str(value) for value in items)
        parts = (
            [rejoined]
            if _PROSE_MULTIVAL_SEPARATOR in rejoined
            else rejoined.split(_MULTIVAL_SEPARATOR)
        )
        for value_idx, value in enumerate(parts):
            yield str(key), value_idx, value


def _gffbase_attribute_rows(cursor) -> Iterator[tuple[str, int, str, int, str | None]]:
    """Stream canonical attributes for every physical gffbase segment."""

    from gffbase.feature import _LazyAttributes

    current: tuple[str, int] | None = None
    blob: bytes | None = None
    normalized: dict[str, list[str]] = {}

    def emit():
        if current is None:
            return
        if blob is not None:
            values = {str(key): list(items) for key, items in _LazyAttributes(blob=blob).items()}
        else:
            values = normalized
        for key, value_idx, value in _canonical_attribute_rows(values):
            yield current[0], current[1], key, value_idx, value

    while rows := cursor.fetchmany(10_000):
        for feature_id, seg_idx, raw_blob, key, value in rows:
            identity = (str(feature_id), int(seg_idx))
            if identity != current:
                yield from emit()
                current = identity
                blob = bytes(raw_blob) if raw_blob is not None else None
                normalized = {}
            if key is not None:
                normalized.setdefault(str(key), []).append(str(value))
    yield from emit()


def _gffutils_signature_components(
    con, *, scratch_dir: Path
) -> tuple[str, int, str, int, str, int, str, int, int, list]:
    """Normalize gffutils in a temporary on-disk SQLite database.

    The temporary tables deliberately trade disk for bounded memory.  They
    preserve gffutils' resolved logical IDs, canonicalize attribute keys
    lexically (while preserving each key's value index), and remove legacy
    GTF relation artifacts.  Resolved IDs are essential under
    ``merge_strategy="create_unique"``: both engines retain incompatible
    duplicate source IDs as ``x``/``x_1``, while each row's ``ID`` attribute
    remains ``x``.  With inference disabled, gffutils records
    dangling ``gene_id``/``transcript_id`` endpoints that are not features;
    with explicit parent records, it also records their identifier attributes
    as self-ancestry (``gene -> gene`` and ``transcript -> transcript``).
    Neither represents a semantic parent relationship.
    """

    import sqlite3

    dialect_rows = con.execute("SELECT dialect FROM meta").fetchmany(1)
    dialect = json.loads(dialect_rows[0][0]) if dialect_rows else {}
    is_gtf = dialect.get("fmt") == "gtf"

    with tempfile.TemporaryDirectory(prefix=".gffbase-signature-", dir=scratch_dir) as directory:
        norm = sqlite3.connect(str(Path(directory) / "normal.sqlite"))
        # Returns no row at all when unset, which is the default.
        previous_temp_row = norm.execute("PRAGMA temp_store_directory").fetchone()
        previous_temp_directory = previous_temp_row[0] if previous_temp_row else ""
        try:
            # Sorting 96M attribute rows spills to disk, and SQLite picks the
            # spill directory itself: the first of `sqlite3_temp_directory`,
            # `SQLITE_TMPDIR`, `TMPDIR`, `/var/tmp`, `/tmp` that it can write.
            # The campaign's bounded child environment carries no TMPDIR, so
            # every sort landed on `/var/tmp` -- a 32 GB root volume, not the
            # fast scratch the databases themselves sit on. That is a capacity
            # cliff as well as a slow one. `temp_store_directory` is the only
            # one of those knobs this build honours (SQLITE_TMPDIR is read but
            # ignored here, verified against 3.53.2), and it sets a process
            # global, hence the restore in `finally`.
            norm.execute(f"PRAGMA temp_store_directory = '{directory}'")
            norm.executescript(
                """
                PRAGMA journal_mode = OFF;
                PRAGMA synchronous = OFF;
                PRAGMA temp_store = FILE;
                PRAGMA cache_size = -65536;
                CREATE TABLE stage (rowid INTEGER, dbid TEXT, logical TEXT, seqid TEXT,
                    source TEXT, featuretype TEXT, start INTEGER, "end" INTEGER,
                    score TEXT, strand TEXT, frame TEXT);
                CREATE TABLE attrs (dbid TEXT, key TEXT, value_idx INTEGER, value TEXT NULL);
                CREATE TABLE relations (parent TEXT, child TEXT, depth INTEGER);
                """
            )
            cursor = con.execute(
                "SELECT rowid, id, seqid, source, featuretype, start, end, score, strand, frame, attributes "
                "FROM features ORDER BY rowid"
            )
            while rows := cursor.fetchmany(10_000):
                stages = []
                attributes: list[tuple[str, str, int, str | None]] = []
                for rowid, dbid, *columns, raw in rows:
                    values = json.loads(raw or "{}")
                    logical = str(dbid)
                    seqid, source, featuretype, start, end, score, strand, frame = columns
                    stages.append(
                        (
                            rowid,
                            str(dbid),
                            logical,
                            seqid,
                            source,
                            featuretype,
                            start,
                            end,
                            score,
                            strand,
                            frame,
                        )
                    )
                    attributes.extend(
                        (str(dbid), key, value_idx, value)
                        for key, value_idx, value in _canonical_attribute_rows(values)
                    )
                norm.executemany(
                    "INSERT INTO stage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", stages
                )
                norm.executemany("INSERT INTO attrs VALUES (?, ?, ?, ?)", attributes)
            cursor = con.execute("SELECT parent, child, level FROM relations")
            while rows := cursor.fetchmany(10_000):
                norm.executemany("INSERT INTO relations VALUES (?, ?, ?)", rows)
            # Everything below joins these tables to each other across 6-12M
            # rows. Built without indexes they degrade to nested-loop scans:
            # the `no-infer` GTF arm on GENCODE (6.07M features, 12.34M
            # relations) never finished a signature in 2h39m. Indexing after
            # the bulk insert rather than before leaves the insert path
            # untouched.
            norm.executescript(
                """
                CREATE INDEX stage_dbid ON stage(dbid);
                CREATE INDEX attrs_dbid ON attrs(dbid);
                CREATE INDEX relations_child ON relations(child);
                CREATE INDEX relations_parent ON relations(parent);
                """
            )
            if is_gtf:
                # gffutils has no persisted synthetic flag.  Explicit GTF
                # parent rows do have attribute-backed relation artifacts,
                # including create_unique cases such as gene ``g_1`` with a
                # raw ``g -> g_1`` depth-2 relation.  Preserve those authored
                # rows and normalize only reserved-source parents for which
                # no such evidence exists.
                norm.execute(
                    """
                    CREATE TABLE authored_parent_artifacts AS
                    SELECT DISTINCT r.parent, r.child, r.depth
                    FROM relations r
                    JOIN stage s ON s.dbid = r.child
                    JOIN attrs a ON a.dbid = s.dbid
                    WHERE (s.featuretype = 'gene' AND r.depth = 2
                           AND a.key = 'gene_id' AND a.value = r.parent)
                       OR (s.featuretype = 'transcript' AND r.depth = 1
                           AND a.key = 'transcript_id' AND a.value = r.parent)
                    """
                )
                # `semantic_relation_filter` probes this table as a correlated
                # NOT EXISTS once per relation row, and the UPDATE below probes
                # it by child. Unindexed, both are a full scan per row.
                norm.executescript(
                    """
                    CREATE INDEX authored_child ON authored_parent_artifacts(child);
                    CREATE INDEX authored_triple
                        ON authored_parent_artifacts(parent, child, depth);
                    """
                )
                norm.execute(
                    """
                    UPDATE stage AS s SET source = 'derived'
                    WHERE s.source IN ('gffbase_derived', 'gffutils_derived')
                      AND s.featuretype IN ('gene', 'transcript')
                      AND NOT EXISTS (
                          SELECT 1 FROM authored_parent_artifacts a
                          WHERE a.child = s.dbid
                      )
                    """
                )
            norm.commit()
            norm.executescript(
                """
                CREATE TABLE segments AS
                SELECT rowid, dbid, logical,
                       ROW_NUMBER() OVER (PARTITION BY logical ORDER BY rowid) - 1 AS seg_idx,
                       seqid, source, featuretype, start, "end", score, strand, frame
                FROM stage;
                CREATE INDEX segments_dbid ON segments(dbid);
                CREATE TABLE feature_ids AS SELECT DISTINCT logical FROM segments;
                CREATE UNIQUE INDEX feature_ids_logical ON feature_ids(logical);
                CREATE TABLE mappings AS SELECT dbid, logical FROM stage;
                CREATE INDEX mappings_dbid ON mappings(dbid);
                """
            )
            segment_sha, segment_count = _hash_cursor(
                norm.execute(
                    'SELECT logical, seg_idx, seqid, source, featuretype, start, "end", score, strand, frame '
                    "FROM segments ORDER BY logical, seg_idx"
                )
            )
            attribute_sha, attribute_count = _hash_cursor(
                norm.execute(
                    "SELECT s.logical, s.seg_idx, a.key, a.value_idx, a.value "
                    "FROM attrs a JOIN segments s ON s.dbid = a.dbid "
                    "ORDER BY s.logical, s.seg_idx, a.key, a.value_idx, a.value"
                )
            )
            endpoint_filter = (
                "JOIN feature_ids fp ON fp.logical = COALESCE(mp.logical, r.parent) "
                "JOIN feature_ids fc ON fc.logical = COALESCE(mc.logical, r.child) "
                if is_gtf
                else ""
            )
            normalized_relations = (
                "FROM relations r "
                "LEFT JOIN mappings mp ON mp.dbid = r.parent "
                "LEFT JOIN mappings mc ON mc.dbid = r.child " + endpoint_filter
            )
            semantic_relation_filter = (
                "NOT EXISTS (SELECT 1 FROM authored_parent_artifacts artifact "
                "WHERE artifact.parent = r.parent AND artifact.child = r.child "
                "AND artifact.depth = r.depth)"
                if is_gtf
                else "1 = 1"
            )
            # The direct edges are the hierarchy; the closure is a function of
            # them. gffutils stores both, but what it stores under `level = 2`
            # is not a closure: `create.py::_update_relations` inserts, for
            # each feature, the children of its children -- a single hop, at a
            # fixed level, with no iteration to a fixed point. On a four-deep
            # chain it records five pairs and omits the sixth. Comparing
            # gffbase's real closure against that cache reported a content
            # divergence on RefSeq (3,218 pairs) and GENCODE GFF3 (108) whose
            # direct edges agreed exactly, and cost both corpora a published
            # speedup. So derive the closure here, from the edges the two
            # engines do agree on, and compare like with like.
            norm.execute(
                "CREATE TABLE direct_edges AS "
                "SELECT DISTINCT COALESCE(mp.logical, r.parent) AS parent, "
                "COALESCE(mc.logical, r.child) AS child "
                + normalized_relations
                + "WHERE r.depth = 1 AND "
                + semantic_relation_filter
            )
            norm.execute("CREATE INDEX direct_edges_parent ON direct_edges(parent)")
            direct_sha, direct_count = _hash_cursor(
                norm.execute("SELECT parent, child FROM direct_edges ORDER BY 1, 2")
            )

            norm.executescript(
                """
                CREATE TABLE closure_pairs (ancestor TEXT, descendant TEXT, depth INTEGER);
                INSERT INTO closure_pairs SELECT parent, child, 1 FROM direct_edges;
                CREATE UNIQUE INDEX closure_pairs_pair ON closure_pairs(ancestor, descendant);
                CREATE TABLE frontier AS SELECT parent AS ancestor, child AS descendant
                    FROM direct_edges;
                """
            )
            # Breadth-first, so the depth a pair is first reached at IS its
            # minimum depth and no GROUP BY is needed. A separate frontier
            # table keeps the closure out of the statement that extends it --
            # SQLite leaves it undefined to read a table an INSERT is writing.
            depth = 1
            while True:
                depth += 1
                if depth > _MAX_CLOSURE_DEPTH:
                    raise ValueError(
                        f"hierarchy exceeds {_MAX_CLOSURE_DEPTH} levels; "
                        "the relation graph is probably cyclic"
                    )
                norm.execute(
                    "CREATE TABLE next_frontier AS "
                    "SELECT DISTINCT f.ancestor AS ancestor, e.child AS descendant "
                    "FROM frontier f JOIN direct_edges e ON e.parent = f.descendant "
                    "WHERE NOT EXISTS (SELECT 1 FROM closure_pairs c "
                    "WHERE c.ancestor = f.ancestor AND c.descendant = e.child)"
                )
                extended = norm.execute("SELECT COUNT(*) FROM next_frontier").fetchone()[0]
                if not extended:
                    norm.execute("DROP TABLE next_frontier")
                    break
                norm.execute(
                    "INSERT OR IGNORE INTO closure_pairs (ancestor, descendant, depth) "
                    "SELECT ancestor, descendant, ? FROM next_frontier",
                    (depth,),
                )
                norm.executescript(
                    "DROP TABLE frontier;ALTER TABLE next_frontier RENAME TO frontier;"
                )
            closure_sha, closure_count = _hash_cursor(
                norm.execute("SELECT ancestor, descendant, depth FROM closure_pairs ORDER BY 1, 2")
            )
            feature_count = int(norm.execute("SELECT COUNT(*) FROM feature_ids").fetchone()[0])
            histogram = [
                [row[0], int(row[1])]
                for row in norm.execute(
                    "SELECT featuretype, COUNT(*) FROM segments WHERE seg_idx = 0 "
                    "GROUP BY featuretype ORDER BY featuretype"
                )
            ]
            return (
                segment_sha,
                segment_count,
                attribute_sha,
                attribute_count,
                direct_sha,
                direct_count,
                closure_sha,
                closure_count,
                feature_count,
                histogram,
            )
        finally:
            # Restore the process global before the directory it names is
            # removed, or the next sort in this process spills into nothing.
            try:
                norm.execute(f"PRAGMA temp_store_directory = '{previous_temp_directory or ''}'")
            except sqlite3.Error:  # pragma: no cover - the connection is going away
                pass
            norm.close()


_SIGNATURE_COMPONENTS = (
    ("segments", "segment_count"),
    ("attributes", "attribute_count"),
    ("direct_relationships", "direct_relationship_count"),
    ("closure", "closure_count"),
)
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _signature_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _is_git_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(char in "0123456789abcdef" for char in value)
    )


def validate_database_signature(signature: dict | None) -> bool:
    """Return whether *signature* is a complete, internally consistent v3.

    A digest alone is not a contract: accepting an object with a forged or
    stale combined digest would let a caller compare two incomplete database
    summaries.  Validate every scalar, histogram and component digest, then
    recompute the digest over precisely the published payload.
    """

    if (
        not isinstance(signature, dict)
        or signature.get("schema_version") != "database-signature-v3"
    ):
        return False
    expected = {
        "schema_version",
        "segment_count",
        "segments_sha256",
        "attribute_count",
        "attributes_sha256",
        "direct_relationship_count",
        "direct_relationships_sha256",
        "closure_count",
        "closure_sha256",
        "feature_count",
        "featuretype_histogram",
        "combined_sha256",
    }
    if set(signature) != expected:
        return False
    for component, count_name in _SIGNATURE_COMPONENTS:
        if not isinstance(signature[count_name], int) or isinstance(signature[count_name], bool):
            return False
        if signature[count_name] < 0 or not _is_sha256(signature[f"{component}_sha256"]):
            return False
    if not isinstance(signature["feature_count"], int) or isinstance(
        signature["feature_count"], bool
    ):
        return False
    if signature["feature_count"] < 0 or not _is_sha256(signature["combined_sha256"]):
        return False
    histogram = signature["featuretype_histogram"]
    if not isinstance(histogram, list):
        return False
    previous = None
    total = 0
    for entry in histogram:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or not entry[0]
            or not isinstance(entry[1], int)
            or isinstance(entry[1], bool)
            or entry[1] < 0
            or (previous is not None and entry[0] <= previous)
        ):
            return False
        previous = entry[0]
        total += entry[1]
    if total != signature["feature_count"]:
        return False
    if signature["segment_count"] < signature["feature_count"]:
        return False
    if signature["closure_count"] < signature["direct_relationship_count"]:
        return False
    if any(entry[1] <= 0 for entry in histogram):
        return False
    if not histogram and signature["feature_count"] != 0:
        return False
    component_counts = [signature[count_name] for _, count_name in _SIGNATURE_COMPONENTS]
    if signature["feature_count"] == 0 and any(component_counts):
        return False
    if signature["segment_count"] == 0 and signature["attribute_count"] != 0:
        return False
    if (signature["direct_relationship_count"] == 0) != (signature["closure_count"] == 0):
        return False
    if any(
        (signature[count_name] == 0) != (signature[f"{component}_sha256"] == _EMPTY_SHA256)
        for component, count_name in _SIGNATURE_COMPONENTS
    ):
        return False
    payload = {key: value for key, value in signature.items() if key != "combined_sha256"}
    return _signature_digest(payload) == signature["combined_sha256"]


def database_signature(path: Path, *, engine: str) -> dict:
    """Digest every logical segment, ordered attribute and hierarchy row.

    The v3 signature deliberately keeps direct edges separate from closure:
    equal reachability does not prove that the database retained the original
    parent relationships, and closure depths must remain the minimum depths.
    """

    path = Path(path)
    con: Any
    if engine == "gffbase":
        import duckdb

        con = duckdb.connect(str(path), read_only=True)
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        is_gtf = meta.get("fmt") == "gtf"
        source_sql = (
            "CASE WHEN is_synthetic = TRUE "
            "AND source IN ('gffbase_derived', 'gffutils_derived') "
            "THEN 'derived' ELSE source END"
            if is_gtf
            else "source"
        )
        # Schema v1 predates multipart features: it has no `segments` or
        # `segments_all`, one row per feature, and no `seg_idx` on
        # `attributes`. The version bridge measures gffbase 0.1.0, which writes
        # exactly that, so signing it is not optional -- every bridge job must
        # carry a valid v3 signature or the campaign rejects it. Reading v1
        # here rather than migrating keeps the measured artifact untouched.
        if meta.get("schema_version") == "1":
            segment_sql = f"""
                SELECT id AS feature_id, 0 AS seg_idx, seqid,
                       {source_sql},
                       featuretype, start, "end", score, strand, frame
                FROM features ORDER BY id
            """
            attribute_sql = """
                SELECT f.id, 0, CAST(f.attributes_blob AS BLOB), a.key, a.value
                FROM features f
                LEFT JOIN attributes a ON a.feature_id = f.id
                ORDER BY f.id, a.key, a.idx, a.value
            """
        else:
            segment_sql = f"""
                SELECT feature_id, seg_idx, seqid,
                       {source_sql},
                       featuretype, start, "end", score, strand, frame
                FROM segments_all ORDER BY feature_id, seg_idx
            """
            attribute_sql = """
                SELECT sa.feature_id, sa.seg_idx, CAST(sa.attributes_blob AS BLOB),
                       a.key, a.value
                FROM segments_all sa
                LEFT JOIN segments sm ON sm.feature_id = sa.feature_id AND sm.seg_idx = sa.seg_idx
                LEFT JOIN attributes a ON a.feature_id = sa.feature_id AND a.seg_idx =
                    CASE WHEN sm.seg_idx > 0 AND sm.attrs_same_as_seg0 THEN 0 ELSE sa.seg_idx END
                ORDER BY sa.feature_id, sa.seg_idx, a.key, a.idx, a.value
            """
        endpoint_joins = (
            "JOIN features p ON p.id = e.parent JOIN features c ON c.id = e.child" if is_gtf else ""
        )
        closure_endpoint_joins = (
            "JOIN features a ON a.id = cl.ancestor JOIN features d ON d.id = cl.descendant"
            if is_gtf
            else ""
        )
        direct_sql = f"""
            SELECT DISTINCT e.parent, e.child FROM edges e {endpoint_joins}
            ORDER BY e.parent, e.child
        """
        closure_sql = f"""
            SELECT cl.ancestor, cl.descendant, MIN(cl.depth) FROM closure cl
            {closure_endpoint_joins}
            GROUP BY cl.ancestor, cl.descendant ORDER BY cl.ancestor, cl.descendant
        """
        feature_count_sql = "SELECT COUNT(*) FROM features"
        histogram_sql = """
            SELECT featuretype, COUNT(*) FROM features
            GROUP BY featuretype ORDER BY featuretype
        """
    elif engine == "gffutils":
        import sqlite3

        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        raise ValueError("engine must be 'gffbase' or 'gffutils'")

    try:
        if engine == "gffbase":
            segment_sha, segment_count = _hash_cursor(con.execute(segment_sql))
            direct_sha, direct_count = _hash_cursor(con.execute(direct_sql))
            closure_sha, closure_count = _hash_cursor(con.execute(closure_sql))
            feature_count_row = con.execute(feature_count_sql).fetchone()
            if feature_count_row is None:  # pragma: no cover - COUNT always returns one row
                raise ValueError("features count query returned no row")
            feature_count = int(feature_count_row[0])
            histogram = [[row[0], int(row[1])] for row in con.execute(histogram_sql).fetchall()]
            attribute_sha, attribute_count = _hash_cursor(
                _RowsCursor(_gffbase_attribute_rows(con.execute(attribute_sql)))
            )
        else:
            (
                segment_sha,
                segment_count,
                attribute_sha,
                attribute_count,
                direct_sha,
                direct_count,
                closure_sha,
                closure_count,
                feature_count,
                histogram,
            ) = _gffutils_signature_components(con, scratch_dir=path.parent)
    finally:
        con.close()

    combined_payload = {
        "schema_version": "database-signature-v3",
        "segment_count": segment_count,
        "segments_sha256": segment_sha,
        "attribute_count": attribute_count,
        "attributes_sha256": attribute_sha,
        "direct_relationship_count": direct_count,
        "direct_relationships_sha256": direct_sha,
        "closure_count": closure_count,
        "closure_sha256": closure_sha,
        "feature_count": feature_count,
        "featuretype_histogram": histogram,
    }
    combined = _signature_digest(combined_payload)
    return {**combined_payload, "combined_sha256": combined}


def signatures_match(left: dict | None, right: dict | None) -> bool | None:
    """Compare signatures, or return ``None`` when either is unavailable."""

    if not left or not right:
        return None
    if not validate_database_signature(left) or not validate_database_signature(right):
        return None
    return left == right


def _positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _numbers_equal(left: object, right: object) -> bool:
    return (
        isinstance(left, (int, float))
        and not isinstance(left, bool)
        and isinstance(right, (int, float))
        and not isinstance(right, bool)
        and math.isfinite(left)
        and math.isfinite(right)
        and math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)
    )


def candidate_evidence_is_valid(
    candidate: dict | None,
    *,
    require_exhaustive: bool = False,
    require_rtree: bool = False,
) -> bool:
    """Validate one candidate's completed ingest evidence without I/O.

    A numeric validation sample is useful for scaling diagnostics, but is not
    publication evidence.  A B-tree fallback is a valid ingest/batched-query
    result, but never a spatial-index measurement.
    """

    if not isinstance(candidate, dict):
        return False
    validation = candidate.get("validation")
    if not isinstance(validation, dict):
        return False
    eligible = validation.get("sample_eligible")
    checked = validation.get("sample_checked")
    requested = validation.get("requested_sample")
    if (
        not isinstance(eligible, int)
        or isinstance(eligible, bool)
        or eligible < 0
        or not isinstance(checked, int)
        or isinstance(checked, bool)
        or checked < 0
    ):
        return False
    if requested == "all":
        coverage_ok = checked == eligible
    elif isinstance(requested, str) and requested.isdigit() and int(requested) > 0:
        coverage_ok = checked == min(int(requested), eligible)
    else:
        return False
    if require_exhaustive and requested != "all":
        return False

    rtree_built = candidate.get("rtree_built")
    if not isinstance(rtree_built, bool) or (require_rtree and not rtree_built):
        return False
    expected_ids = list(FULL_VALIDATION_IDS)
    skipped = validation.get("skipped")
    if rtree_built:
        expected_ids.insert(7, "INV-8")
        skipped_ok = skipped == []
    else:
        skipped_ok = skipped == ["INV-8 (bbox_matches): no R-tree was built for this database"]

    signature = candidate.get("correctness_signature")
    feature_count = candidate.get("n_features")
    if not isinstance(signature, dict):
        return False
    return (
        candidate.get("state") == "completed"
        and candidate.get("exit_code") == 0
        and _positive_number(candidate.get("wall_seconds"))
        and isinstance(feature_count, int)
        and not isinstance(feature_count, bool)
        and feature_count >= 0
        and validation.get("ok") is True
        and validation.get("level") == "full"
        and validation.get("errors") == []
        and skipped_ok
        and validation.get("checked_ids") == expected_ids
        and coverage_ok
        and validate_database_signature(signature)
        and signature.get("feature_count") == feature_count
    )


def _contains_private_path(value: object) -> bool:
    """Return whether a portable result recursively carries an absolute path."""

    if isinstance(value, dict):
        return any(
            _contains_private_path(key) or _contains_private_path(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_private_path(item) for item in value)
    if not isinstance(value, str):
        return False
    if "file://" in value or "~/" in value or "~\\" in value:
        return True
    if any(
        char in "/\\" and (index == 0 or not value[index - 1].isalnum())
        for index, char in enumerate(value)
    ):
        return True
    return any(
        value[index].isalpha() and value[index + 1] == ":" and value[index + 2] in "/\\"
        for index in range(len(value) - 2)
    )


def _contains_nonfinite_number(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_nonfinite_number(key) or _contains_nonfinite_number(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_nonfinite_number(item) for item in value)
    return isinstance(value, float) and not math.isfinite(value)


def _exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _portable_relative_path(value: object) -> bool:
    if not _nonempty_string(value) or not isinstance(value, str):
        return False
    if _contains_private_path(value) or "\\" in value or "://" in value or "\x00" in value:
        return False
    return all(part not in {"", ".", ".."} for part in value.split("/"))


def _portable_filename(value: object) -> bool:
    return (
        _portable_relative_path(value)
        and isinstance(value, str)
        and "/" not in value
        and value not in {".", ".."}
    )


def _normalized_utc_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value
    ):
        return None
    normalized = value[:-6] + "Z" if value.endswith("+00:00") else value
    try:
        time.strptime(normalized, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return normalized if value.endswith(("Z", "+00:00")) else None


def _utc_timestamp_is_valid(value: object) -> bool:
    return _normalized_utc_timestamp(value) is not None


def _bounded_env_is_valid(value: object, *, threads: int | None = None) -> bool:
    if not isinstance(value, dict) or set(value) != set(_BENCHMARK_ENV_KEYS):
        return False
    if not all(isinstance(item, str) for item in value.values()):
        return False
    if threads is not None and value != benchmark_env(threads):
        return False
    return all(value[key] == "1" for key in _BENCHMARK_ENV_KEYS[2:])


def _timing_is_valid(timing: object, wall_seconds: object, *, repeats: int) -> bool:
    if not isinstance(timing, dict) or not _positive_number(wall_seconds):
        return False
    count = timing.get("n")
    if not _positive_int(count) or count != repeats:
        return False
    if count == 1:
        return (
            set(timing) == {"value", "n"}
            and _positive_number(timing.get("value"))
            and _numbers_equal(timing["value"], wall_seconds)
        )
    values = timing.get("values")
    return (
        set(timing) == {"median", "min", "max", "values", "n"}
        and isinstance(values, list)
        and len(values) == count
        and all(_positive_number(value) for value in values)
        and _positive_number(timing.get("min"))
        and _positive_number(timing.get("median"))
        and _positive_number(timing.get("max"))
        and _numbers_equal(timing["min"], min(values))
        and _numbers_equal(timing["median"], statistics.median(values))
        and _numbers_equal(timing["max"], max(values))
        and _numbers_equal(timing["median"], wall_seconds)
    )


def _query_evidence_error(section: str, value: object, candidate: dict, params: dict) -> str | None:
    if not isinstance(value, dict):
        return f"{section} is not an object"
    if value.get("state") == "skipped":
        if not _exact_keys(value, {"state", "reason"}) or not _nonempty_string(value.get("reason")):
            return f"{section} skipped shape is invalid"
        return None
    if value.get("state") != "completed":
        return f"{section} state is invalid"
    if section == "spatial":
        if not candidate_evidence_is_valid(candidate, require_exhaustive=True, require_rtree=True):
            return "spatial result lacks an R-tree-backed candidate"
        expected = {
            "state",
            "n_queries",
            "wall_seconds",
            "qps",
            "total_features_returned",
            "timing",
        }
        count_name = "n_queries"
    else:
        expected = {"state", "n_anchors", "n_descendants", "wall_seconds", "qps", "timing"}
        count_name = "n_anchors"
    if not _exact_keys(value, expected):
        return f"{section} completed shape is invalid"
    count = value.get(count_name)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return f"{section} count is invalid"
    if section == "spatial":
        returned = value.get("total_features_returned")
        if not _nonnegative_int(returned):
            return "spatial returned count is invalid"
        if count != params["n_spatial"]:
            return "spatial query count differs from params"
    else:
        if not _nonnegative_int(value.get("n_descendants")):
            return "batched descendant count is invalid"
        if count > params["n_batched"]:
            return "batched anchor count exceeds params"
    wall = value.get("wall_seconds")
    if not _timing_is_valid(value.get("timing"), wall, repeats=params["repeats"]):
        return f"{section} timing is invalid"
    qps = value.get("qps")
    if (
        not isinstance(wall, (int, float))
        or isinstance(wall, bool)
        or not _positive_number(wall)
        or not _positive_number(qps)
    ):
        return f"{section} qps is invalid"
    if not _numbers_equal(qps, count / float(wall)):
        return f"{section} qps is invalid"
    return None


def _rss_is_valid(value: dict, *, allow_zero: bool) -> bool:
    rss_bytes = value.get("peak_rss_bytes")
    rss_mb = value.get("peak_rss_mb")
    bytes_ok = _nonnegative_int(rss_bytes) if allow_zero else _positive_int(rss_bytes)
    return (
        bytes_ok
        and isinstance(rss_bytes, int)
        and _numbers_equal(rss_mb, rss_bytes / (1024 * 1024))
    )


def _validation_warning_is_valid(value: object) -> bool:
    expected = {"invariant", "name", "severity", "count", "detail", "examples"}
    return (
        _exact_keys(value, expected)
        and isinstance(value, dict)
        and all(_nonempty_string(value.get(key)) for key in ("invariant", "name", "detail"))
        and value.get("severity") == "warning"
        and _positive_int(value.get("count"))
        and isinstance(value.get("examples"), list)
        and len(value["examples"]) <= 5
    )


def _python_runtime_version(value: object) -> tuple[int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"3\.(10|11|12|13|14)\.(?:0|[1-9]\d*)", value)
    if not match:
        return None
    return 3, int(match.group(1))


def _normalized_architecture(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    aliases = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    return aliases.get(value.casefold().replace("-", "_"))


def _runtime_platform_identity(platform_name: object, machine: object) -> tuple[str, str] | None:
    if not isinstance(platform_name, str):
        return None
    operating_system = {
        "linux": "linux",
        "windows": "windows",
        "macos": "macos",
    }.get(platform_name.partition("-")[0].casefold())
    architecture = _normalized_architecture(machine)
    if operating_system is None or architecture is None:
        return None
    return operating_system, architecture


def _libc_identity_is_valid(value: object, *, operating_system: str) -> bool:
    if not _exact_keys(value, {"family", "version"}) or not isinstance(value, dict):
        return False
    family = value.get("family")
    version = value.get("version")
    if operating_system != "linux":
        return family is None and version is None
    return (
        family in {"glibc", "musl"}
        and isinstance(version, str)
        and bool(re.fullmatch(r"(?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))+", version))
    )


def _wheel_platform_identity(tag: str) -> tuple[str, str] | None:
    linux = re.fullmatch(
        r"(?P<policy>manylinux(?:_\d+_\d+|1|2010|2014)|musllinux_\d+_\d+|linux)"
        r"_(?P<arch>x86_64|aarch64)",
        tag,
    )
    if linux:
        policy = linux.group("policy")
        versioned = re.fullmatch(r"(manylinux|musllinux)_(\d+)_(\d+)", policy)
        if versioned:
            family, major, minor = (
                versioned.group(1),
                int(versioned.group(2)),
                int(versioned.group(3)),
            )
            if (family == "manylinux" and (major != 2 or minor < 5)) or (
                family == "musllinux" and (major != 1 or minor < 1)
            ):
                return None
        return "linux", "x86_64" if linux.group("arch") == "x86_64" else "aarch64"
    windows = re.fullmatch(r"win_(amd64|arm64)", tag)
    if windows:
        return "windows", "x86_64" if windows.group(1) == "amd64" else "aarch64"
    macos = re.fullmatch(r"macosx_\d+_\d+_(x86_64|arm64)", tag)
    if macos:
        return "macos", "x86_64" if macos.group(1) == "x86_64" else "aarch64"
    return None


def _native_module_matches_runtime(
    native_module: object,
    *,
    operating_system: str,
    architecture: str,
    runtime_tag: str,
    wheel_abi_tag: str,
    wheel_platform_tag: str,
    runtime_libc: object,
) -> bool:
    if not isinstance(native_module, str):
        return False
    if wheel_abi_tag == "abi3":
        # Stable-ABI module suffixes omit architecture. The already-validated
        # singleton wheel platform supplies OS/architecture; the module name
        # must independently identify the same ABI and native library family.
        if operating_system in {"linux", "macos"}:
            return native_module == "_native.abi3.so"
        if operating_system == "windows":
            return native_module == "_native.pyd"
        return False
    if operating_system == "linux":
        match = re.fullmatch(
            r"_native\.cpython-(3\d+)-(x86_64|aarch64)-linux-(gnu|musl)\.so",
            native_module,
        )
        if not match:
            return False
        native_libc = match.group(3)
        libc_family = runtime_libc.get("family") if isinstance(runtime_libc, dict) else None
        expected_native_libc = (
            {"glibc": "gnu", "musl": "musl"}.get(libc_family)
            if isinstance(libc_family, str)
            else None
        )
        libc_matches = not (
            (wheel_platform_tag.startswith("manylinux") and native_libc != "gnu")
            or (wheel_platform_tag.startswith("musllinux") and native_libc != "musl")
        )
        return (
            f"cp{match.group(1)}" == runtime_tag
            and _normalized_architecture(match.group(2)) == architecture
            and native_libc == expected_native_libc
            and libc_matches
        )
    if operating_system == "windows":
        match = re.fullmatch(r"_native\.cp(3\d+)-win_(amd64|arm64)\.pyd", native_module)
        if not match:
            return False
        return f"cp{match.group(1)}" == runtime_tag and (
            _normalized_architecture(match.group(2)) == architecture
        )
    if operating_system == "macos":
        match = re.fullmatch(r"_native\.cpython-(3\d+)-darwin\.so", native_module)
        return bool(match and f"cp{match.group(1)}" == runtime_tag)
    return False


def _candidate_wheel_tags(wheel: object) -> tuple[str, str, str] | None:
    if not isinstance(wheel, str) or not wheel.endswith(".whl"):
        return None
    wheel_stem = wheel.removesuffix(".whl")
    filename_parts = wheel_stem.rsplit("-", 3)
    if len(filename_parts) != 4 or wheel_stem.count("-") not in {4, 5}:
        return None
    distribution_version, python_tag, abi_tag, platform_tag = filename_parts
    distribution_version_prefix = f"gffbase-{_CANDIDATE_VERSION}"
    if distribution_version != distribution_version_prefix:
        build_prefix = f"{distribution_version_prefix}-"
        if not distribution_version.startswith(build_prefix) or not re.fullmatch(
            r"[0-9][^\r\n]*", distribution_version.removeprefix(build_prefix)
        ):
            return None
    # Compressed tag sets are valid wheel syntax, but ambiguous provenance:
    # this record must identify the one ABI and platform actually measured.
    if any("." in tag for tag in (python_tag, abi_tag, platform_tag)):
        return None
    return python_tag, abi_tag, platform_tag


def _linux_wheel_matches_libc(platform_tag: str, libc: object) -> bool:
    if not isinstance(libc, dict):
        return False
    family = libc.get("family")
    version = libc.get("version")
    if not isinstance(version, str):
        return False
    runtime_version = tuple(int(component) for component in version.split("."))
    if platform_tag.startswith("linux_"):
        return family in {"glibc", "musl"}
    legacy_manylinux = {
        "manylinux1": (2, 5),
        "manylinux2010": (2, 12),
        "manylinux2014": (2, 17),
    }
    prefix = platform_tag.rsplit("_", 2)[0]
    if prefix in legacy_manylinux:
        return family == "glibc" and runtime_version >= legacy_manylinux[prefix]
    manylinux = re.fullmatch(r"manylinux_(\d+)_(\d+)_(?:x86_64|aarch64)", platform_tag)
    if manylinux:
        required = (int(manylinux.group(1)), int(manylinux.group(2)))
        return (
            required[0] == 2
            and required[1] >= 5
            and family == "glibc"
            and runtime_version >= required
        )
    musllinux = re.fullmatch(r"musllinux_(\d+)_(\d+)_(?:x86_64|aarch64)", platform_tag)
    if musllinux:
        required = (int(musllinux.group(1)), int(musllinux.group(2)))
        return (
            required[0] == 1
            and required[1] >= 1
            and family == "musl"
            and runtime_version >= required
        )
    return False


def _artifact_matches_runtime(
    wheel: object,
    native_module: object,
    python_version: object,
    platform_name: object,
    machine: object,
    libc: object,
) -> bool:
    runtime = _python_runtime_version(python_version)
    platform_identity = _runtime_platform_identity(platform_name, machine)
    filename_tags = _candidate_wheel_tags(wheel)
    if runtime is None or platform_identity is None or filename_tags is None:
        return False
    python_tag, abi_tag, platform_tag = filename_tags
    runtime_tag = f"cp{runtime[0]}{runtime[1]}"
    if abi_tag == "abi3":
        python_abi_matches = python_tag == "cp310" and runtime >= (3, 10)
    else:
        python_abi_matches = python_tag == runtime_tag and abi_tag == runtime_tag
    if not python_abi_matches or _wheel_platform_identity(platform_tag) != platform_identity:
        return False
    if platform_identity[0] == "linux" and not _linux_wheel_matches_libc(platform_tag, libc):
        return False
    return _native_module_matches_runtime(
        native_module,
        operating_system=platform_identity[0],
        architecture=platform_identity[1],
        runtime_tag=runtime_tag,
        wheel_abi_tag=abi_tag,
        wheel_platform_tag=platform_tag,
        runtime_libc=libc,
    )


def _artifact_evidence_is_valid(
    artifact: dict,
    install: dict,
    python: dict,
    *,
    platform_name: object,
    machine: object,
    libc: object,
) -> bool:
    if not _exact_keys(artifact, {"wheel", "wheel_sha256", "metadata", "wheel_tags", "native"}):
        return False
    wheel = artifact.get("wheel")
    metadata = artifact.get("metadata")
    wheel_tags = artifact.get("wheel_tags")
    native = artifact.get("native")
    filename_tags = _candidate_wheel_tags(wheel)
    if (
        not isinstance(wheel, str)
        or not _portable_filename(wheel)
        or filename_tags is None
        or not _is_sha256(artifact.get("wheel_sha256"))
        or not _exact_keys(metadata, {"name", "version"})
        or not isinstance(metadata, dict)
        or metadata.get("name") != "gffbase"
        or metadata.get("version") != _CANDIDATE_VERSION
        or wheel_tags != ["-".join(filename_tags)]
        or not _exact_keys(native, {"member", "sha256"})
        or not isinstance(native, dict)
    ):
        return False
    native_member = native.get("member")
    native_module = install.get("native_module")
    return (
        isinstance(native_member, str)
        and isinstance(native_module, str)
        and _portable_relative_path(native_member)
        and native_member == f"gffbase/{native_module}"
        and _is_sha256(native.get("sha256"))
        and native.get("sha256") == install.get("native_sha256")
        and _artifact_matches_runtime(
            wheel,
            native_module,
            python.get("version"),
            platform_name,
            machine,
            libc,
        )
    )


def _candidate_shape_is_valid(candidate: dict) -> bool:
    expected = {
        "label",
        "peak_rss_bytes",
        "peak_rss_mb",
        "exit_code",
        "state",
        "benchmark_env",
        "cap_seconds",
        "wall_seconds",
        "n_features",
        "rtree_built",
        "correctness_signature",
        "fmt",
        "validation",
        "disk_bytes",
    }
    validation_keys = {
        "ok",
        "level",
        "checked",
        "skipped",
        "errors",
        "warnings",
        "requested_sample",
        "sample_eligible",
        "sample_checked",
        "checked_ids",
    }
    validation = candidate.get("validation")
    if not isinstance(validation, dict):
        return False
    checked_ids = validation.get("checked_ids")
    checked = validation.get("checked")
    warnings = validation.get("warnings")
    expected_checked = len(FULL_VALIDATION_IDS) + int(candidate.get("rtree_built") is True)
    return (
        _exact_keys(candidate, expected)
        and _nonempty_string(candidate.get("label"))
        and _rss_is_valid(candidate, allow_zero=False)
        and isinstance(candidate.get("exit_code"), int)
        and not isinstance(candidate.get("exit_code"), bool)
        and candidate["exit_code"] == 0
        and candidate.get("state") == "completed"
        and _positive_int(candidate.get("cap_seconds"))
        and _positive_number(candidate.get("wall_seconds"))
        and _positive_int(candidate.get("n_features"))
        and _positive_int(candidate.get("disk_bytes"))
        and candidate.get("fmt") in {"gff3", "gtf"}
        and _exact_keys(validation, validation_keys)
        and isinstance(checked, list)
        and len(checked) == expected_checked
        and all(_nonempty_string(item) for item in checked)
        and isinstance(checked_ids, list)
        and len(checked_ids) == expected_checked
        and isinstance(warnings, list)
        and all(_validation_warning_is_valid(item) for item in warnings)
        and validation.get("errors") == []
        and _nonnegative_int(validation.get("sample_eligible"))
        and _nonnegative_int(validation.get("sample_checked"))
    )


def _legacy_shape_is_valid(legacy: dict) -> bool:
    base = {
        "label",
        "peak_rss_bytes",
        "peak_rss_mb",
        "exit_code",
        "state",
        "benchmark_env",
        "cap_seconds",
        "wall_seconds",
        "disk_bytes",
    }
    if legacy.get("state") == "completed":
        return (
            _exact_keys(legacy, base | {"n_features", "correctness_signature"})
            and _nonempty_string(legacy.get("label"))
            and _rss_is_valid(legacy, allow_zero=False)
            and isinstance(legacy.get("exit_code"), int)
            and not isinstance(legacy.get("exit_code"), bool)
            and legacy["exit_code"] == 0
            and _positive_int(legacy.get("cap_seconds"))
            and _positive_number(legacy.get("wall_seconds"))
            and _positive_int(legacy.get("disk_bytes"))
            and _positive_int(legacy.get("n_features"))
            and validate_database_signature(legacy.get("correctness_signature"))
        )
    if legacy.get("state") == "timed_out":
        diagnostics = {"stdout_bytes", "stdout_sha256", "stdout_parse_error"}
        return (
            _exact_keys(legacy, base | diagnostics)
            and _nonempty_string(legacy.get("label"))
            and _rss_is_valid(legacy, allow_zero=True)
            and isinstance(legacy.get("exit_code"), int)
            and not isinstance(legacy.get("exit_code"), bool)
            and legacy.get("exit_code") != 0
            and _positive_int(legacy.get("cap_seconds"))
            and legacy.get("wall_seconds") is None
            and _nonnegative_int(legacy.get("disk_bytes"))
            and _nonnegative_int(legacy.get("stdout_bytes"))
            and _is_sha256(legacy.get("stdout_sha256"))
            and legacy.get("stdout_parse_error") == "invalid final JSON object"
        )
    return False


def benchmark_candidate_evidence_is_valid(
    candidate: dict | None,
    *,
    require_exhaustive: bool = False,
    require_rtree: bool = False,
) -> bool:
    """Validate the complete closed candidate row emitted by ``06_mega``.

    ``candidate_evidence_is_valid`` is intentionally usable by the ingest
    harness before every diagnostic field has been assembled.  Campaign
    acceptance is a stronger boundary: it also requires the frozen exact row
    shape, resource measurements, caps, and validation metadata.
    """

    return bool(
        isinstance(candidate, dict)
        and _candidate_shape_is_valid(candidate)
        and candidate_evidence_is_valid(
            candidate,
            require_exhaustive=require_exhaustive,
            require_rtree=require_rtree,
        )
    )


def benchmark_comparator_evidence_is_valid(comparator: dict | None) -> bool:
    """Return whether a comparator is one closed completed/timeout variant."""

    return bool(isinstance(comparator, dict) and _legacy_shape_is_valid(comparator))


def benchmark_query_evidence_error(
    section: str,
    value: object,
    candidate: dict,
    params: dict,
) -> str | None:
    """Validate a frozen spatial or batched query record without I/O."""

    if section not in {"spatial", "batched"}:
        return "query section is invalid"
    return _query_evidence_error(section, value, candidate, params)


def _environment_is_valid(value: object, *, threads: int) -> bool:
    required = {
        "timestamp_utc",
        "git_commit",
        "git_dirty",
        "hostname",
        "platform",
        "machine",
        "libc",
        "cpu_model",
        "cpu_cores_physical",
        "cpu_cores_logical",
        "total_ram_bytes",
        "free_disk_bytes",
        "cpu_affinity",
        "python",
        "rustc_version",
        "packages",
        "gffbase_install",
        "artifact",
        "env",
    }
    if not isinstance(value, dict) or not _exact_keys(value, required):
        return False
    python = value.get("python")
    packages = value.get("packages")
    install = value.get("gffbase_install")
    artifact = value.get("artifact")
    if (
        not isinstance(python, dict)
        or not isinstance(packages, dict)
        or not isinstance(install, dict)
        or not isinstance(artifact, dict)
    ):
        return False
    package_keys = {"gffbase", "duckdb", "pyarrow", "pandas", "polars", "gffutils", "psutil"}
    install_keys = {
        "distribution_version",
        "python_version",
        "python_module",
        "native_version",
        "native_module",
        "native_sha256",
    }
    affinity = value.get("cpu_affinity")
    physical = value.get("cpu_cores_physical")
    logical = value.get("cpu_cores_logical")
    platform_identity = _runtime_platform_identity(value.get("platform"), value.get("machine"))
    return (
        _utc_timestamp_is_valid(value.get("timestamp_utc"))
        and _is_git_commit(value.get("git_commit"))
        and value.get("git_dirty") is False
        and all(
            _nonempty_string(value.get(key))
            for key in ("hostname", "platform", "machine", "cpu_model", "rustc_version")
        )
        and _positive_int(physical)
        and _positive_int(logical)
        and isinstance(logical, int)
        and isinstance(physical, int)
        and logical >= physical
        and _positive_int(value.get("total_ram_bytes"))
        and _positive_int(value.get("free_disk_bytes"))
        and platform_identity is not None
        and _libc_identity_is_valid(value.get("libc"), operating_system=platform_identity[0])
        and isinstance(affinity, list)
        and bool(affinity)
        and all(_nonnegative_int(cpu) for cpu in affinity)
        and len(affinity) == len(set(affinity))
        and len(affinity) <= logical
        and all(cpu < logical for cpu in affinity)
        and _exact_keys(python, {"version", "implementation", "executable"})
        and _nonempty_string(python.get("version"))
        and python.get("implementation") == "CPython"
        and _portable_filename(python.get("executable"))
        and isinstance(python.get("executable"), str)
        and python["executable"].startswith("python")
        and _exact_keys(packages, package_keys)
        and all(_nonempty_string(packages.get(key)) for key in package_keys)
        and packages.get("gffbase") == _CANDIDATE_VERSION
        and packages.get("gffutils") == _COMPARATOR_VERSION
        and _exact_keys(install, install_keys)
        and all(
            _nonempty_string(install.get(key))
            for key in ("distribution_version", "python_version", "native_version")
        )
        and install["distribution_version"] == _CANDIDATE_VERSION
        and install["python_version"] == _CANDIDATE_VERSION
        and install["native_version"] == _CANDIDATE_VERSION
        and install.get("python_module") == "__init__.py"
        and _portable_filename(install.get("native_module"))
        and isinstance(install.get("native_module"), str)
        and install["native_module"].startswith("_native.")
        and install["native_module"].endswith((".so", ".pyd"))
        and _is_sha256(install.get("native_sha256"))
        and _artifact_evidence_is_valid(
            artifact,
            install,
            python,
            platform_name=value.get("platform"),
            machine=value.get("machine"),
            libc=value.get("libc"),
        )
        and _bounded_env_is_valid(value.get("env"), threads=threads)
    )


def benchmark_environment_evidence_is_valid(value: object, *, threads: int) -> bool:
    """Validate the complete closed primary-harness environment record."""

    return _environment_is_valid(value, threads=threads)


def benchmark_bounded_environment_is_valid(value: object, *, threads: int) -> bool:
    """Validate the exact ten-variable benchmark child environment."""

    return _bounded_env_is_valid(value, threads=threads)


def benchmark_row_evidence_error(row: dict | None) -> str | None:
    """Return why a schema-v3 benchmark row is not publishable, else ``None``.

    This is the one pure gate shared by the harness's direct publisher and
    the Markdown renderer.  It recomputes a published ratio from completed,
    signature-equivalent ingests instead of trusting a serialized number.
    """

    if not isinstance(row, dict):
        return "row is not an object"
    row_keys = {
        "name",
        "key",
        "measured",
        "input",
        "input_bytes",
        "input_sha256",
        "feature_lines",
        "gffbase",
        "legacy",
        "ingest_speedup",
        "spatial",
        "batched",
        "db_paths",
        "params",
    }
    if not _exact_keys(row, row_keys):
        return "row shape is invalid"
    if _contains_private_path(row):
        return "row contains an absolute path"
    key = row.get("key")
    corpus = _PRIMARY_CORPORA.get(key) if isinstance(key, str) else None
    if corpus is None:
        return "row is not a canonical primary corpus"
    if row.get("name") != corpus["name"]:
        return "row name differs from the canonical corpus"
    input_path = row.get("input")
    if (
        not _portable_relative_path(input_path)
        or not isinstance(input_path, str)
        or input_path.rsplit("/", 1)[-1] != corpus["filename"]
        or row.get("input_bytes") != corpus["bytes"]
        or row.get("input_sha256") != corpus["sha256"]
        or not _positive_int(row.get("feature_lines"))
    ):
        return "row source identity is invalid"
    candidate = row.get("gffbase")
    if not candidate_evidence_is_valid(candidate, require_exhaustive=True):
        return "candidate is incomplete or invalid"
    if not isinstance(candidate, dict):  # keeps this pure seam safe for untyped payloads
        return "candidate is incomplete or invalid"
    if not _candidate_shape_is_valid(candidate):
        return "candidate shape is invalid"
    params = row.get("params")
    if not isinstance(params, dict) or not _exact_keys(
        params,
        {
            "legacy_cap_seconds",
            "gffbase_cap_seconds",
            "n_spatial",
            "n_batched",
            "repeats",
            "region_seed",
            "threads",
            "gtf_arm",
            "infer_gtf_parents",
            "validation_sample",
            "benchmark_env",
        },
    ):
        return "params shape is invalid"
    threads = params.get("threads")
    if not _positive_int(threads):
        return "params threads are invalid"
    for name in (
        "legacy_cap_seconds",
        "gffbase_cap_seconds",
        "n_spatial",
        "n_batched",
        "repeats",
    ):
        if not _positive_int(params.get(name)):
            return f"params {name} is invalid"
    if params.get("region_seed") != 20260501:
        return "params region seed is invalid"
    if params.get("validation_sample") != "all":
        return "params validation sample is not exhaustive"
    if key == "gencode-gtf":
        if params.get("gtf_arm") != "no-infer" or params.get("infer_gtf_parents") is not False:
            return "GENCODE GTF arm is not the canonical no-infer comparison"
    elif params.get("gtf_arm") is not None or params.get("infer_gtf_parents") is not None:
        return "non-GTF corpus carries GTF controls"
    if not _bounded_env_is_valid(params.get("benchmark_env"), threads=threads):
        return "params benchmark environment is invalid"
    if candidate.get("benchmark_env") != params["benchmark_env"]:
        return "candidate benchmark environment differs from params"
    if candidate.get("cap_seconds") != params.get("gffbase_cap_seconds"):
        return "candidate cap differs from params"
    if candidate.get("fmt") != corpus["fmt"]:
        return "candidate format differs from corpus"
    if candidate["validation"].get("requested_sample") != params["validation_sample"]:
        return "candidate validation sample differs from params"
    if candidate["label"] != f"gffbase ingest({corpus['filename']})":
        return "candidate label does not identify the corpus"
    measured = row.get("measured")
    if not _exact_keys(measured, {"timestamp_utc", "git_commit", "git_dirty"}):
        return "measured shape is invalid"
    if (
        not isinstance(measured, dict)
        or not _utc_timestamp_is_valid(measured.get("timestamp_utc"))
        or not _is_git_commit(measured.get("git_commit"))
        or measured.get("git_dirty") is not False
    ):
        return "measured identity is invalid"
    db_paths = row.get("db_paths")
    if (
        not isinstance(db_paths, dict)
        or not _exact_keys(db_paths, {"gffbase", "legacy"})
        or not all(_portable_relative_path(path) for path in db_paths.values())
        or not isinstance(db_paths.get("gffbase"), str)
        or not isinstance(db_paths.get("legacy"), str)
        or db_paths["gffbase"].rsplit("/", 1)[-1] != f"{key}.duckdb"
        or db_paths["legacy"].rsplit("/", 1)[-1] != f"{key}_legacy.sqlite"
    ):
        return "database paths are invalid"
    spatial_error = _query_evidence_error("spatial", row.get("spatial"), candidate, params)
    if spatial_error:
        return spatial_error
    batched_error = _query_evidence_error("batched", row.get("batched"), candidate, params)
    if batched_error:
        return batched_error
    legacy = row.get("legacy")
    if not isinstance(legacy, dict):
        return "comparator is missing"
    if not _legacy_shape_is_valid(legacy):
        return "legacy shape is invalid"
    if legacy.get("benchmark_env") != params["benchmark_env"]:
        return "comparator benchmark environment differs from params"
    if legacy.get("cap_seconds") != params.get("legacy_cap_seconds"):
        return "comparator cap differs from params"
    if legacy["label"] != f"legacy gffutils ingest({corpus['filename']})":
        return "comparator label does not identify the corpus"
    speedup = row.get("ingest_speedup")
    if legacy.get("state") == "timed_out":
        if (
            legacy.get("wall_seconds") is not None
            or not _positive_int(legacy.get("cap_seconds"))
            or speedup is not None
        ):
            return "timed-out comparator is not censored"
        return None
    if legacy.get("state") != "completed" or legacy.get("exit_code") != 0:
        return "comparator is incomplete"
    if not _positive_number(legacy.get("wall_seconds")):
        return "comparator has no completed wall time"
    legacy_wall = legacy.get("wall_seconds")
    candidate_wall = candidate.get("wall_seconds")
    if (
        not isinstance(legacy_wall, (int, float))
        or isinstance(legacy_wall, bool)
        or not isinstance(candidate_wall, (int, float))
        or isinstance(candidate_wall, bool)
        or not _positive_number(legacy_wall)
        or not _positive_number(candidate_wall)
    ):
        return "completed wall time is invalid"
    if (
        signatures_match(
            candidate.get("correctness_signature"), legacy.get("correctness_signature")
        )
        is not True
    ):
        return "candidate and comparator signatures differ"
    if candidate.get("n_features") != legacy.get("n_features"):
        return "candidate and comparator feature counts differ"
    expected = float(legacy_wall) / float(candidate_wall)
    if not _positive_number(speedup) or not _numbers_equal(speedup, expected):
        return "ingest speedup does not match completed evidence"
    return None


def benchmark_results_evidence_error(data: dict | None) -> str | None:
    """Return why a complete schema-v3 result payload is unpublishable."""

    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return "unsupported benchmark schema"
    if set(data) != {"schema_version", "environment", "corpora"}:
        return "result shape is invalid"
    if _contains_private_path(data):
        return "result contains an absolute path or private file URI"
    if _contains_nonfinite_number(data):
        return "result contains a nonfinite number"
    corpora = data.get("corpora")
    primary = set(_PRIMARY_CORPORA)
    if not isinstance(corpora, dict) or set(corpora) != primary:
        return "result must contain exactly the primary corpora"
    threads = None
    for key, row in corpora.items():
        error = benchmark_row_evidence_error(row)
        if error:
            return f"{key}: {error}"
        if row.get("key") != key:
            return f"{key}: row key differs from corpus key"
        row_threads = row["params"]["threads"]
        if threads is None:
            threads = row_threads
        elif threads != row_threads:
            return "corpus thread controls differ"
    environment_value = data.get("environment")
    if not isinstance(threads, int) or not _environment_is_valid(
        environment_value, threads=threads
    ):
        return "environment is invalid or differs from row controls"
    if not isinstance(environment_value, dict):  # narrowed by the validator above
        return "environment is invalid"
    for key, row in corpora.items():
        measured = row["measured"]
        params = row["params"]
        measured_timestamp = _normalized_utc_timestamp(measured["timestamp_utc"])
        environment_timestamp = _normalized_utc_timestamp(environment_value["timestamp_utc"])
        if (
            measured_timestamp is None
            or environment_timestamp is None
            or measured_timestamp > environment_timestamp
        ):
            return f"{key}: measured timestamp is later than environment"
        if measured["git_commit"] != environment_value["git_commit"]:
            return f"{key}: measured commit differs from environment"
        if measured["git_dirty"] is not False or environment_value["git_dirty"] is not False:
            return f"{key}: dirty benchmark evidence is not publishable"
        if params["benchmark_env"] != environment_value["env"]:
            return f"{key}: measured controls differ from environment"
    return None


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def pretty_seconds(s: float) -> str:
    if s < 1:
        return f"{s * 1000:.1f} ms"
    if s < 60:
        return f"{s:.2f} s"
    m, s = divmod(s, 60)
    return f"{int(m)} min {s:.1f} s"


def pretty_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024:
            return f"{n:.2f} {unit}"
    return f"{n:.2f} TB"


def pretty_qps(qps: float) -> str:
    return f"{qps:.0f} qps" if qps >= 1 else f"{qps:.3f} qps"


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


def atomic_write_json(path: Path, payload: object) -> Path:
    """Durably replace a JSON file without exposing a partial document."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            tmp_name = handle.name
            json.dump(payload, handle, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass
    return path


@contextmanager
def _result_lock(path: Path):
    """Serialize legacy merge callers; cluster jobs never share this path."""

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # 0600 explicitly: a cluster job's results live in the campaign's attempt
    # scratch, whose scan rejects any allowed file that is not private, and
    # `open("a+")` yields 0644 under the usual umask. The file is deliberately
    # left behind -- see `worker.py`'s allowlist and the note there.
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        # Guarded like `fcntl` below: Windows has no `os.fchmod` before 3.13,
        # and its POSIX mode bits mean nothing there anyway.
        if hasattr(os, "fchmod"):
            os.fchmod(handle.fileno(), 0o600)
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:  # pragma: no cover - Windows fallback
            pass
        try:
            yield
        finally:
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows fallback
                pass


def write_results(
    stage: str, payload: dict, *, benchmark_controls: dict[str, str] | None = None
) -> Path:
    """Write a stage's results, replacing the file. Stamps provenance."""
    OUT.mkdir(parents=True, exist_ok=True)
    body = {
        "schema_version": SCHEMA_VERSION,
        "environment": environment(benchmark_controls=benchmark_controls),
        **payload,
    }
    path = OUT / f"{stage}.json"
    return atomic_write_json(path, body)


def merge_results(
    stage: str,
    section: str,
    entries: dict,
    *,
    benchmark_controls: dict[str, str] | None = None,
) -> Path:
    """Update `entries` inside `<stage>.json[section]`, preserving the rest.

    This is the fix for the defect that destroyed the published record. The
    mega benchmark used to build a payload containing only the corpora
    selected by `--only` and write the WHOLE file, so each targeted re-run
    silently deleted every other corpus's results. `06_mega.json` ended up
    holding one of five corpora while the performance document named it as the
    provenance for all five, and four rows had no surviving measurement at all.

    Merging by key means a `--only mane` run updates MANE and touches nothing
    else, which is what anyone would assume it already did.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{stage}.json"
    with _result_lock(path):
        existing: dict = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text())
            except (OSError, ValueError):
                existing = {}
        if existing.get("schema_version") not in (None, SCHEMA_VERSION):
            # Different shape: start clean rather than blend two schemas.
            existing = {}
        merged = dict(existing.get(section) or {})
        merged.update(entries)
        body = {
            "schema_version": SCHEMA_VERSION,
            "environment": environment(benchmark_controls=benchmark_controls),
            **{
                k: v
                for k, v in existing.items()
                if k not in {"schema_version", "environment", section}
            },
            section: merged,
        }
        return atomic_write_json(path, body)
