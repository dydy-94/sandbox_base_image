#!/bin/bash
# preflight-wheels.sh
#
# Verifies that `requirements/*.txt` can be satisfied entirely from the
# `wheels/` directory (no network needed).  Use BEFORE you kick off a
# v10 build so you can find out which wheel(s) you're missing without
# waiting for the build itself.
#
# Usage:
#   bash preflight-wheels.sh <wheels-dir> <requirements-dir>
#   bash preflight-wheels.sh            # use defaults: ./wheels ./requirements
#
# Exit code:
#   0  -> the offline build will succeed (all transitive deps satisfiable)
#   1  -> build will block or fall back to network; missing dependencies
#         are listed
#   2  -> required tools missing (python3 / pip) — install then re-run

set -e

WHEELS_DIR="${1:-wheels}"
REQS_DIR="${2:-requirements}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERR: python3 not in PATH" >&2; exit 2
fi
PYTHON="$(command -v python3 || command -v python || true)"

if [ -z "$PYTHON" ]; then
    echo "ERR: python3 / python missing" >&2; exit 2
fi

# Make sure pip is usable in the current python.
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    "$PYTHON" -m ensurepip --user 2>/dev/null || "$PYTHON" -m pip --version >/dev/null 2>&1 || {
        echo "ERR: pip not available in $PYTHON" >&2
        exit 2
    }
fi

# Pick the requirements files we'll analyse.
mapfile -t REQS_FILES < <(find "$REQS_DIR" -maxdepth 1 -name '*.txt' -type f | sort)
if [ "${#REQS_FILES[@]}" -eq 0 ]; then
    echo "ERR: no requirements/*.txt files under '$REQS_DIR'" >&2
    exit 2
fi

# Verify the wheels directory has at least one .whl.
WHEEL_COUNT="$(find "$WHEELS_DIR" -maxdepth 1 -name '*.whl' -type f 2>/dev/null | wc -l)"
echo "Wheels directory: $WHEELS_DIR — $WHEEL_COUNT wheels"
if [ "$WHEEL_COUNT" -eq 0 ]; then
    echo "WARN: no wheels in $WHEELS_DIR — pre-flight cannot run meaningfully."
fi

echo
echo "== Trying pip install --dry-run --no-index --find-links=$WHEELS_DIR =="
echo

# Run pip --dry-run, capture output. Don't fail the script if pip returns
# non-zero — we want to inspect the message.
OVERALL=0
for REQ in "${REQS_FILES[@]}"; do
    echo "-- requirements: $REQ --"
    set +e
    OUT="$("$PYTHON" -m pip install \
        --dry-run \
        --no-cache-dir \
        --no-index \
        --find-links "$WHEELS_DIR" \
        -r "$REQ" 2>&1)"
    RC=$?
    set -e
    if [ $RC -eq 0 ]; then
        echo "OK ($?)"
    else
        echo "EXITED $RC:"
        echo "$OUT" | tail -20
        OVERALL=1
    fi
    echo
done

if [ $OVERALL -eq 0 ]; then
    echo "== preflight OK: every requirements tier is satisfiable from $WHEELS_DIR =="
    exit 0
fi
echo
echo "== preflight FAILED: at least one tier cannot be satisfied offline =="
echo "== Expected fix: add the missing wheel(s) to $WHEELS_DIR =="
echo "==   usually by re-running scripts/prepare-wheels.sh against $REQS_DIR =="
exit 1
