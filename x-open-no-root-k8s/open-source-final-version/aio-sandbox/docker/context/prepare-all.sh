#!/bin/bash
# prepare-all.sh — unified entry point that orchestrates ALL offline asset
# preparation for the AIO Sandbox final-version image.
#
# What it does (in dependency order):
#   1. prepare-apt-archives.sh   — fetches 769+ apt .deb files
#                                   (incl. chrome/noVNC/websocat/code-server)
#   2. prepare-wheels.sh         — fetches python-server runtime pip wheels
#   3. prepare-rust.sh           — fetches rustup-init + agent-browser source
#   4. prepare-npm.sh            — fetches aio/static-assets npm tarballs
#   5. prepare-daytona.sh        — fetches daytona + computer-use binaries
#
# After this script finishes, you can build the image with NO network
# access (assuming the CN-mirrors stay reachable for the few packages that
# the Dockerfile's `apt-get install` runs as a non-offline fallback — see
# the README for details).
#
# Run from anywhere:
#   bash docker/context/prepare-all.sh
#
# Or, from the docker/context/ directory:
#   ./prepare-all.sh
#
# Override individual sub-scripts by setting SKIP_<NAME>=1:
#   SKIP_NPM=1 SKIP_RUST=1 bash prepare-all.sh
#
# All downloads go to CN mirrors (TUNA / aliyun / npmmirror) first, with
# upstream fallbacks. If you have an internal corporate mirror (e.g.
# cmbchina jaf), set the corresponding env var (APT_MIRROR / NPM_REGISTRY
# / PIP_INDEX_URL) before running.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Make every sub-script executable in case the file was checked out
# without the +x bit.
chmod +x prepare-*.sh 2>/dev/null || true

LOG_DIR="$SCRIPT_DIR/.prepare-logs"
mkdir -p "$LOG_DIR"

# Pretty status helper.
step() {
    local n="$1"; shift
    local name="$1"; shift
    local script="$1"; shift
    local skip_var="SKIP_$(echo "$name" | tr 'a-z-' 'A-Z_')"
    if [ "${!skip_var:-0}" = "1" ]; then
        echo
        echo "===== [$n/5] $name  SKIPPED (${skip_var}=1) ====="
        return 0
    fi
    echo
    echo "===== [$n/5] $name  ($script) ====="
    local log="$LOG_DIR/${name}.log"
    if bash "$script" 2>&1 | tee "$log"; then
        echo "===== [$n/5] $name  OK ====="
    else
        local rc=$?
        echo "===== [$n/5] $name  FAILED (rc=$rc; full log: $log) ====="
        return $rc
    fi
}

T0=$(date +%s)

step 1 apt    prepare-apt-archives.sh
step 2 wheels prepare-wheels.sh
step 3 rust   prepare-rust.sh
step 4 npm    prepare-npm.sh
step 5 daytona prepare-daytona.sh

T1=$(date +%s)
ELAPSED=$((T1 - T0))
echo
echo "============================================================="
echo "All offline assets prepared in ${ELAPSED}s."
echo "Logs:  $LOG_DIR/"
echo
echo "Asset summary:"
for d in apt-archives wheels rustup-pre cargo-vendored npm-tgz bin dist; do
    if [ -d "$SCRIPT_DIR/$d" ]; then
        n=$(find "$SCRIPT_DIR/$d" -maxdepth 2 -type f | wc -l)
        s=$(du -sh "$SCRIPT_DIR/$d" 2>/dev/null | awk '{print $1}')
        printf '  %-22s %4d files, %s\n' "$d" "$n" "$s"
    fi
done
echo
echo "Now build the image with:"
echo "  cd .."
echo "  docker buildx build -f Dockerfile.final -t aio-sandbox:final-test --load ."