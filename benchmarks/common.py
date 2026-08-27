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
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": _cmd("git", "-C", str(ROOT), "rev-parse", "HEAD"),
        "git_dirty": bool(_cmd("git", "-C", str(ROOT), "status", "--porcelain")),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "machine": platform.machine(),
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
        "gffbase_install": _installed_gffbase(),
        "artifact": {
            "wheel": (
                Path(os.environ["GFFBASE_BENCH_WHEEL"]).name
                if os.environ.get("GFFBASE_BENCH_WHEEL")
                else None
            ),
            "wheel_sha256": os.environ.get("GFFBASE_BENCH_WHEEL_SHA256"),
        },
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
        if parse_error is not None:
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


def _canonical_attribute_rows(values: dict) -> Iterator[tuple[str, int, str | None]]:
    """Yield a deterministic semantic representation of column nine.

    GFF/GTF attribute key order is not semantic and the two engines expose it
    differently for inferred parents.  Keys are therefore sorted, while the
    order of values for each key is retained.  A key with no values is emitted
    with index ``-1`` and ``None`` so flags such as ``pseudo`` remain covered.
    """

    for key in sorted(values):
        raw_values = values[key]
        items = raw_values if isinstance(raw_values, (list, tuple)) else [raw_values]
        if not items:
            yield str(key), -1, None
            continue
        for value_idx, value in enumerate(items):
            yield str(key), value_idx, str(value)


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
        try:
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
                    # The projects use different reserved source labels for
                    # the same inferred GTF parents.  Treat both labels as one
                    # engine-neutral semantic source.
                    if source in {"gffbase_derived", "gffutils_derived"}:
                        source = "derived"
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
            norm.commit()
            norm.executescript(
                """
                CREATE INDEX stage_dbid ON stage(dbid);
                CREATE TABLE segments AS
                SELECT rowid, dbid, logical,
                       ROW_NUMBER() OVER (PARTITION BY logical ORDER BY rowid) - 1 AS seg_idx,
                       seqid, source, featuretype, start, "end", score, strand, frame
                FROM stage;
                CREATE INDEX segments_dbid ON segments(dbid);
                CREATE TABLE feature_ids AS SELECT DISTINCT logical FROM segments;
                CREATE TABLE mappings AS SELECT dbid, logical FROM stage;
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
                "COALESCE(mp.logical, r.parent) <> COALESCE(mc.logical, r.child)"
                if is_gtf
                else "1 = 1"
            )
            direct_sha, direct_count = _hash_cursor(
                norm.execute(
                    "SELECT DISTINCT COALESCE(mp.logical, r.parent), "
                    "COALESCE(mc.logical, r.child) "
                    + normalized_relations
                    + "WHERE r.depth = 1 AND "
                    + semantic_relation_filter
                    + " ORDER BY 1, 2"
                )
            )
            closure_sha, closure_count = _hash_cursor(
                norm.execute(
                    "SELECT COALESCE(mp.logical, r.parent), COALESCE(mc.logical, r.child), "
                    "MIN(r.depth) "
                    + normalized_relations
                    + "WHERE "
                    + semantic_relation_filter
                    + " GROUP BY 1, 2 ORDER BY 1, 2"
                )
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
            norm.close()


_SIGNATURE_COMPONENTS = (
    ("segments", "segment_count"),
    ("attributes", "attribute_count"),
    ("direct_relationships", "direct_relationship_count"),
    ("closure", "closure_count"),
)


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
        fmt_row = con.execute("SELECT value FROM meta WHERE key = 'fmt'").fetchone()
        is_gtf = bool(fmt_row and fmt_row[0] == "gtf")
        segment_sql = """
            SELECT feature_id, seg_idx, seqid,
                   CASE WHEN source IN ('gffbase_derived', 'gffutils_derived')
                        THEN 'derived' ELSE source END,
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
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


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
        return any(_contains_private_path(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_private_path(item) for item in value)
    return isinstance(value, str) and (
        value.startswith(("/", "\\\\"))
        or (len(value) > 2 and value[1] == ":" and value[2] in "/\\")
    )


def _exact_keys(value: object, keys: set[str]) -> bool:
    return isinstance(value, dict) and set(value) == keys


def _bounded_env_is_valid(value: object, *, threads: int | None = None) -> bool:
    if not isinstance(value, dict) or set(value) != set(_BENCHMARK_ENV_KEYS):
        return False
    if not all(isinstance(item, str) for item in value.values()):
        return False
    if threads is not None and value != benchmark_env(threads):
        return False
    return all(value[key] == "1" for key in _BENCHMARK_ENV_KEYS[2:])


def _timing_is_valid(timing: object, wall_seconds: object) -> bool:
    if not isinstance(timing, dict) or not _positive_number(wall_seconds):
        return False
    count = timing.get("n")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return False
    if count == 1:
        return (
            set(timing) == {"value", "n"}
            and _positive_number(timing.get("value"))
            and timing["value"] == wall_seconds
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
        and timing["min"] <= timing["median"] <= timing["max"]
        and timing["median"] == wall_seconds
    )


def _query_evidence_error(section: str, value: object, candidate: dict) -> str | None:
    if not isinstance(value, dict):
        return f"{section} is not an object"
    if value.get("state") == "skipped":
        if not _exact_keys(value, {"state", "reason"}) or not isinstance(value.get("reason"), str):
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
        if not isinstance(returned, int) or isinstance(returned, bool) or returned < 0:
            return "spatial returned count is invalid"
    elif not isinstance(value.get("n_descendants"), int) or value["n_descendants"] < 0:
        return "batched descendant count is invalid"
    wall = value.get("wall_seconds")
    if not _timing_is_valid(value.get("timing"), wall):
        return f"{section} timing is invalid"
    qps = value.get("qps")
    if (
        not isinstance(wall, (int, float))
        or not isinstance(qps, (int, float))
        or not _positive_number(qps)
        or abs(qps - count / wall) > 1e-12
    ):
        return f"{section} qps is invalid"
    return None


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
    return (
        _exact_keys(candidate, expected)
        and _positive_number(candidate.get("cap_seconds"))
        and isinstance(candidate.get("disk_bytes"), int)
        and candidate["disk_bytes"] >= 0
        and isinstance(candidate.get("fmt"), str)
        and _exact_keys(candidate.get("validation"), validation_keys)
        and isinstance(candidate["validation"].get("checked"), list)
        and isinstance(candidate["validation"].get("warnings"), list)
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
            and legacy.get("exit_code") == 0
            and _positive_number(legacy.get("wall_seconds"))
        )
    if legacy.get("state") == "timed_out":
        optional_stdout = {"stdout_bytes", "stdout_sha256", "stdout_parse_error"}
        return (
            set(legacy).issubset(base | optional_stdout)
            and base.issubset(legacy)
            and legacy.get("exit_code") != 0
            and legacy.get("wall_seconds") is None
            and not {"n_features", "correctness_signature"}.intersection(legacy)
        )
    return False


def _environment_is_valid(value: object, *, threads: int) -> bool:
    required = {
        "timestamp_utc",
        "git_commit",
        "git_dirty",
        "hostname",
        "platform",
        "machine",
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
    if not isinstance(python, dict):
        return False
    return (
        _exact_keys(python, {"version", "implementation", "executable"})
        and all(
            isinstance(python.get(key), str) for key in ("version", "implementation", "executable")
        )
        and "/" not in python["executable"]
        and _bounded_env_is_valid(value.get("env"), threads=threads)
    )


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
    if not isinstance(threads, int) or isinstance(threads, bool) or threads < 1:
        return "params threads are invalid"
    if not _bounded_env_is_valid(params.get("benchmark_env"), threads=threads):
        return "params benchmark environment is invalid"
    if candidate.get("benchmark_env") != params["benchmark_env"]:
        return "candidate benchmark environment differs from params"
    if candidate.get("cap_seconds") != params.get("gffbase_cap_seconds"):
        return "candidate cap differs from params"
    for name in ("n_spatial", "n_batched", "repeats", "region_seed"):
        if not isinstance(params.get(name), int) or params[name] < 1:
            return f"params {name} is invalid"
    measured = row.get("measured")
    if not _exact_keys(measured, {"timestamp_utc", "git_commit", "git_dirty"}):
        return "measured shape is invalid"
    db_paths = row.get("db_paths")
    if (
        not isinstance(db_paths, dict)
        or not _exact_keys(db_paths, {"gffbase", "legacy"})
        or not all(
            isinstance(path, str) and not _contains_private_path(path) for path in db_paths.values()
        )
    ):
        return "database paths are invalid"
    for name in ("name", "key", "input", "input_sha256"):
        if not isinstance(row.get(name), str):
            return f"row {name} is invalid"
    if not _is_sha256(row.get("input_sha256")) or not all(
        isinstance(row.get(name), int) and row[name] >= 0
        for name in ("input_bytes", "feature_lines")
    ):
        return "row input evidence is invalid"
    spatial_error = _query_evidence_error("spatial", row.get("spatial"), candidate)
    if spatial_error:
        return spatial_error
    batched_error = _query_evidence_error("batched", row.get("batched"), candidate)
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
    speedup = row.get("ingest_speedup")
    if legacy.get("state") == "timed_out":
        if (
            legacy.get("wall_seconds") is not None
            or not _positive_number(legacy.get("cap_seconds"))
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
    if not isinstance(legacy_wall, (int, float)) or not isinstance(candidate_wall, (int, float)):
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
    expected = legacy_wall / candidate_wall
    if (
        not _positive_number(speedup)
        or not isinstance(speedup, (int, float))
        or abs(speedup - expected) > max(1e-12, abs(expected) * 1e-12)
    ):
        return "ingest speedup does not match completed evidence"
    return None


def benchmark_results_evidence_error(data: dict | None) -> str | None:
    """Return why a complete schema-v3 result payload is unpublishable."""

    if not isinstance(data, dict) or str(data.get("schema_version")) != SCHEMA_VERSION:
        return "unsupported benchmark schema"
    if set(data) != {"schema_version", "environment", "corpora"}:
        return "result shape is invalid"
    corpora = data.get("corpora")
    primary = {"mane", "chess", "refseq", "gencode-gtf", "gencode-gff3"}
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
    if not isinstance(threads, int) or not _environment_is_valid(
        data.get("environment"), threads=threads
    ):
        return "environment is invalid or differs from row controls"
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
    with lock_path.open("a+") as handle:
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
