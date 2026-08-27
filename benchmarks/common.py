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
from contextlib import contextmanager
from pathlib import Path

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

    Reading a gzip stream to EOF also verifies its trailer/CRC.  The returned
    dictionary is suitable for embedding verbatim in run provenance.
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


def environment() -> dict:
    """Everything needed to reproduce or fairly compare a run.

    None of this was recorded before: the published numbers carried their
    hardware and versions only as hand-typed prose in a markdown file, which
    said "gffbase 0.1.0" for the entire 0.2.0 development cycle. A benchmark
    result that does not say what it ran on is not a measurement, and one that
    does not say what version it measured cannot detect a regression.
    """
    import psutil

    free = shutil.disk_usage(str(OUT if OUT.exists() else BENCH_DIR)).free
    try:
        affinity = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = None
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
        "env": {k: os.environ[k] for k in ("GFFBASE_TEST_DISABLE_RTREE",) if k in os.environ},
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
    info: dict = {
        "label": label,
        "peak_rss_bytes": peak,
        "peak_rss_mb": peak / (1024 * 1024),
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "state": "timed_out" if timed_out else "completed",
        "benchmark_env": applied_env,
    }
    last = text.splitlines()[-1] if text else ""
    try:
        info.update(json.loads(last))
    except (ValueError, json.JSONDecodeError):
        info["raw_stdout_tail"] = text[-1000:]
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


def _blob_attribute_rows(rows) -> list[tuple[str, int, int, str, int, str]]:
    """Expand every physical segment's column-nine values in stored order."""

    from gffbase._pyfallback.attributes import parse_attributes

    out = []
    for feature_id, seg_idx, blob in rows:
        text = bytes(blob or b"").decode("utf-8", errors="replace")
        pairs, _ = parse_attributes(text)
        out.extend(
            (str(feature_id), int(seg_idx), ordinal, str(key), int(value_idx), str(value))
            for ordinal, (key, value, value_idx) in enumerate(pairs)
        )
    return out


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
    if engine == "gffbase":
        import duckdb

        con = duckdb.connect(str(path), read_only=True)
        segment_sql = """
            SELECT feature_id, seg_idx, seqid, source, featuretype, start, "end", score, strand, frame
            FROM segments_all ORDER BY feature_id, seg_idx
        """
        attribute_sql = """
            SELECT feature_id, seg_idx, attributes_blob
            FROM segments_all ORDER BY feature_id, seg_idx
        """
        direct_sql = "SELECT parent, child FROM edges ORDER BY parent, child"
        closure_sql = """
            SELECT ancestor, descendant, depth FROM closure
            ORDER BY ancestor, descendant, depth
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
            feature_count = int(con.execute(feature_count_sql).fetchone()[0])
            histogram = [[row[0], int(row[1])] for row in con.execute(histogram_sql).fetchall()]
            attribute_rows = _blob_attribute_rows(con.execute(attribute_sql).fetchall())
            attribute_sha, attribute_count = _hash_cursor(_RowsCursor(attribute_rows))
        else:
            # gffutils represents each physical line as a separate feature,
            # including a duplicate-ID suffix in the database key.  Recover
            # the source ID from column nine and number those physical rows as
            # segments, matching gffbase's compact multipart representation.
            rows = con.execute(
                "SELECT rowid, id, seqid, source, featuretype, start, end, score, strand, frame, attributes "
                "FROM features ORDER BY rowid"
            ).fetchall()
            logical_by_dbid: dict[str, str] = {}
            parsed_rows = []
            for row in rows:
                rowid, dbid, *columns, raw = row
                values = json.loads(raw or "{}")
                source_id = values.get("ID")
                logical = str(
                    source_id[0] if isinstance(source_id, list) and source_id else source_id or dbid
                )
                logical_by_dbid[str(dbid)] = logical
                parsed_rows.append((rowid, logical, columns, values))
            segment_index: dict[str, int] = {}
            segment_rows = []
            attribute_rows: list[tuple[str, int, int, str, int, str]] = []
            logical_types: dict[str, str] = {}
            for _, logical, columns, values in parsed_rows:
                seg_idx = segment_index.get(logical, 0)
                segment_index[logical] = seg_idx + 1
                seqid, source, featuretype, start, end, score, strand, frame = columns
                segment_rows.append(
                    (logical, seg_idx, seqid, source, featuretype, start, end, score, strand, frame)
                )
                logical_types.setdefault(logical, str(featuretype))
                for key_ord, (key, item_values) in enumerate(values.items()):
                    if not isinstance(item_values, list):
                        item_values = [item_values]
                    attribute_rows.extend(
                        (logical, seg_idx, key_ord, str(key), idx, str(value))
                        for idx, value in enumerate(item_values)
                    )
            direct_rows = {
                (
                    logical_by_dbid.get(str(parent), str(parent)),
                    logical_by_dbid.get(str(child), str(child)),
                )
                for parent, child in con.execute(
                    "SELECT parent, child FROM relations WHERE level = 1"
                )
            }
            closure_depths: dict[tuple[str, str], int] = {}
            for parent, child, depth in con.execute("SELECT parent, child, level FROM relations"):
                key = (
                    logical_by_dbid.get(str(parent), str(parent)),
                    logical_by_dbid.get(str(child), str(child)),
                )
                closure_depths[key] = min(closure_depths.get(key, int(depth)), int(depth))
            segment_rows.sort(key=lambda row: (row[0], row[1]))
            attribute_rows.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4], row[5]))
            segment_sha, segment_count = _hash_cursor(_RowsCursor(segment_rows))
            direct_sha, direct_count = _hash_cursor(_RowsCursor(sorted(direct_rows)))
            closure_sha, closure_count = _hash_cursor(
                _RowsCursor(
                    [
                        (parent, child, depth)
                        for (parent, child), depth in sorted(closure_depths.items())
                    ]
                )
            )
            feature_count = len(logical_types)
            histogram_counts: dict[str, int] = {}
            for featuretype in logical_types.values():
                histogram_counts[featuretype] = histogram_counts.get(featuretype, 0) + 1
            histogram = [
                [featuretype, count] for featuretype, count in sorted(histogram_counts.items())
            ]
            attribute_sha, attribute_count = _hash_cursor(_RowsCursor(attribute_rows))
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


def write_results(stage: str, payload: dict) -> Path:
    """Write a stage's results, replacing the file. Stamps provenance."""
    OUT.mkdir(parents=True, exist_ok=True)
    body = {"schema_version": SCHEMA_VERSION, "environment": environment(), **payload}
    path = OUT / f"{stage}.json"
    return atomic_write_json(path, body)


def merge_results(stage: str, section: str, entries: dict) -> Path:
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
            "environment": environment(),
            **{
                k: v
                for k, v in existing.items()
                if k not in {"schema_version", "environment", section}
            },
            section: merged,
        }
        return atomic_write_json(path, body)
