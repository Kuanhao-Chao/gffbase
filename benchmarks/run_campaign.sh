#!/usr/bin/env bash
#
# Run the full benchmark campaign, end to end, on a host with >= 50 cores.
#
#     tmux new-session -d -s gffbench 'bash benchmarks/run_campaign.sh'
#     tmux attach -t gffbench
#
# Everything it needs already exists under $STAGE (shared storage), built by an
# earlier session: three hermetic conda interpreters and a candidate wheel. This
# script verifies them rather than rebuilding, then drives the four campaign
# phases. It is safe to re-run: preflight is transactional and workers resume.
#
# Why not just run it anywhere:
#
#   * 50 CORES, EXACTLY. `campaign/model.py` maps one 10-CPU lane to each thread
#     count in (1, 2, 4, 8, 10), so the sweep needs CPUs 0-49 allowed, online,
#     and distinct physical cores. salz1 has 48 and cannot host it.
#
#   * GLIBC IS FORWARD-ONLY. The extension links against the glibc of whatever
#     host built it. A wheel built on an el9 node (glibc 2.34) fails to load on
#     an el8 node (2.28) with `version GLIBC_2.34 not found`, and gffbase then
#     falls back to the pure-Python parser -- which would silently benchmark the
#     wrong engine. This wheel is built on the OLDEST node on purpose, so it
#     loads everywhere. The check below is what makes that non-silent.
#
#   * NO PYTHONPATH. The campaign runs children with a bounded environment, so
#     what is measured is the installed artifact, not the working tree.
set -euo pipefail

REPO=/ccb/salz3/kh.chao/gffbase
STAGE=/ccb/salz3/kh.chao/.gffbase-tmp
RUN_ID="${RUN_ID:-linux-$(date +%Y%m%d)-v020rc1}"
WHEEL="$STAGE/wheels/gffbase-0.2.0rc1-cp310-abi3-linux_x86_64.whl"
PRIMARY="$STAGE/conda_primary/bin/python3.11"
GFFBASE_010="$STAGE/conda_gffbase010/bin/python3.11"
GFFUTILS_013="$STAGE/conda_gffutils013/bin/python3.11"

cd "$REPO"
export TMPDIR="$STAGE"

say() { printf '\n=== %s ===\n' "$*"; }

say "host"
printf '  %s  cores=%s  glibc=%s\n' "$(hostname -s)" "$(nproc)" \
       "$(ldd --version | head -1 | grep -oE '[0-9]+\.[0-9]+$')"

say "preconditions"
"$PRIMARY" - <<'PY'
import os, sys
allowed = set(os.sched_getaffinity(0))
missing = sorted(set(range(50)) - allowed)
if missing:
    sys.exit(f"  FAIL: campaign needs CPUs 0-49; missing {missing}. "
             f"This host allows {len(allowed)}.")
print(f"  CPUs 0-49 available (host allows {len(allowed)})")
PY

for role in primary:"$PRIMARY" gffbase010:"$GFFBASE_010" gffutils013:"$GFFUTILS_013"; do
    label="${role%%:*}"; interp="${role#*:}"
    [ -x "$interp" ] || { echo "  FAIL: missing interpreter $interp"; exit 1; }
    [ -L "$interp" ] && { echo "  FAIL: $interp is a symlink; the campaign refuses one"; exit 1; }
    printf '  %-12s ' "$label"
    "$interp" - <<'PY'
import importlib.metadata as m
found = []
for pkg in ("gffbase", "gffutils"):
    try:
        found.append(f"{pkg}=={m.version(pkg)}")
    except Exception:
        pass
native = None
try:
    import gffbase
    native = gffbase.native_available()
except Exception:
    pass
print(" ".join(found), "| native:", native)
PY
done

# The one that has actually bitten: a wheel built on a newer host loads nothing
# and the benchmark silently measures the pure-Python fallback.
"$PRIMARY" - <<'PY'
import sys, gffbase
if not gffbase.native_available():
    sys.exit("  FAIL: the primary interpreter cannot load the native extension. "
             "Rebuild the wheel ON THIS HOST:\n"
             "    cargo clean --manifest-path rust/Cargo.toml\n"
             "    python -m maturin build --release --manifest-path rust/Cargo.toml \\\n"
             "        --compatibility linux -o $TMPDIR/wheels\n"
             "  Benchmarking the fallback would make every number meaningless.")
PY

[ -f "$WHEEL" ] || { echo "  FAIL: candidate wheel not found at $WHEEL"; exit 1; }
echo "  candidate wheel present"

# The campaign root and every component of it must be private mode 0700, and a
# directory created under a setgid parent inherits 2700 -- which the check
# rejects. Clear it here rather than failing five minutes in.
mkdir -p benchmarks/out/cluster
chmod 0700 benchmarks/out benchmarks/out/cluster
chmod g-s  benchmarks/out benchmarks/out/cluster 2>/dev/null || true

say "1/4 preflight  (hashes every corpus, probes each interpreter, builds the parent-stripped GTF control)"
"$PRIMARY" benchmarks/cluster_campaign.py preflight \
    --run-id "$RUN_ID" \
    --candidate-wheel "$WHEEL" \
    --primary-python "$PRIMARY" \
    --gffbase-010-python "$GFFBASE_010" \
    --gffutils-013-python "$GFFUTILS_013" \
    --prepare-parent-stripped \
    --execute

say "2/4 canonical  (the 11 jobs that produce the published numbers; legacy runs uncapped here)"
"$PRIMARY" benchmarks/cluster_campaign.py canonical --run-id "$RUN_ID" --execute

say "3/4 launch  (25 exploratory thread-scaling jobs, five tmux workers on disjoint lanes)"
"$PRIMARY" benchmarks/cluster_campaign.py launch --run-id "$RUN_ID" --execute

say "4/4 status"
"$PRIMARY" benchmarks/cluster_campaign.py status --run-id "$RUN_ID"

cat <<'NEXT'

=== next ===
Workers run in their own tmux sessions. Watch them with:

    python benchmarks/cluster_campaign.py status --run-id <RUN_ID>

When every job reports completed, merge and publish:

    python benchmarks/cluster_campaign.py merge --run-id <RUN_ID> --publish --execute
    python tools/gen_benchmark_tables.py --write
    make -C docs html SPHINXOPTS="-W --keep-going"

`merge --publish` is the trust boundary: it validates every attempt against the
schema-v3 evidence contract, refuses a spatial number whose run had no R-tree,
and censors a timed-out comparator instead of inventing a wall time for it.
NEXT
