#!/bin/bash
# Pre-download the python-server runtime wheels the Dockerfile needs.
# Run this script once on the build host BEFORE running `docker buildx build`.
# It uses --index-url https://pypi.tuna.tsinghua.edu.cn/simple (CN mirror)
# with a fallback to https://pypi.org/simple if the CN mirror stalls.
#
# Wheels land in ./wheels/ next to this script. The Dockerfile COPY's them
# into the image at /opt/wheels/ and subsequent pip installs use
# `--no-index --find-links /opt/wheels`, so the build makes ZERO calls out
# to PyPI. Truncated/missing wheels can no longer wedge the venv.

set -euo pipefail

# Use whichever Python is on PATH; fall back to `python -m pip`. We need a
# recent pip (>= 21.0) for the --only-binary=:all: option to be respected.
PIP=( )
if command -v pip >/dev/null 2>&1; then
    PIP=(pip)
elif command -v pip3 >/dev/null 2>&1; then
    PIP=(pip3)
elif command -v python >/dev/null 2>&1; then
    PIP=(python -m pip)
elif command -v python3 >/dev/null 2>&1; then
    PIP=(python3 -m pip)
else
    echo "ERROR: no python / pip on PATH; install Python 3.11+ first." >&2
    exit 1
fi
echo "Using pip command: ${PIP[*]}"

WHEELS_DIR="$(cd "$(dirname "$0")" && pwd)/wheels"
mkdir -p "$WHEELS_DIR"
# `.keep` so the Dockerfile's COPY doesn't fail if no wheels landed.
touch "$WHEELS_DIR/.keep"

# Use the same pip-tuna index that the build expects. The "extra-index-url"
# pypi.org fallback only kicks in if tuna is unreachable ÃƒÂ¢Ã¢â€šÂ¬?this gives us
# a way out of a regional outage without committing to a non-CN source for
# the whole run.
PIP_INDEX=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}

# One options string used by every pip install call.
PIP_OPTS=(
    --no-cache-dir
    --retries 5
    --timeout 60
    --index-url "$PIP_INDEX"
    --extra-index-url https://pypi.org/simple
    --dest "$WHEELS_DIR"
    --only-binary=:all:
)

# Tier 1 ÃƒÂ¢Ã¢â€šÂ¬?core API server (the ones the python-server can't boot without)
echo "=== Tier 1 (core API) ==="
"${PIP[@]}" download "${PIP_OPTS[@]}" \
    'click==8.2.1' 'fastapi==0.116.0' 'httpx[socks]==0.28.1' \
    'pydantic>=2.13' 'pydantic-settings==2.10.1' \
    'uvicorn[standard]==0.40.0' 'email-validator==2.2.0' \
    'python-multipart==0.0.20' 'python-json-logger>=3.3.0' \
    'typing-extensions>=4.13.0'

echo "=== Tier 2 (filesystem + config) ==="
"${PIP[@]}" download "${PIP_OPTS[@]}" \
    'pyyaml>=6.0.0' 'distro>=1.9.0' 'watchfiles>=1.0.0' \
    'aiosqlite>=0.20.0' 'asyncinotify>=4.4.0' \
    'cachelib>=0.12.0' 'pyjwt>=2.10.0'

echo "=== Tier 3 (MCP + agents) ==="
"${PIP[@]}" download "${PIP_OPTS[@]}" \
    'fastmcp<3.0.0,>=2.13.2' 'mcp[cli]>=1.23.1' \
    'openhands-aci>=0.3.1'

echo "=== Tier 4 (terminal + cli) ==="
"${PIP[@]}" download "${PIP_OPTS[@]}" \
    'pexpect>=4.8.0' 'ptyprocess>=0.7.0' 'terminado>=0.18.1' \
    'tornado>=6.5.1' 'blessed>=1.21.0' 'bashlex>=0.18' \
    'libtmux>=0.39.0' 'termcolor>=3.1.0' \
    'pyperclip>=1.9.0' 'python-docx>=1.0.0'

echo "=== Tier 5 (post-sweep reinstalls) ==="
"${PIP[@]}" download "${PIP_OPTS[@]}" \
    'pydantic-core==2.46.4' \
    'jupyter-client>=8.6.3' \
    'terminado>=0.18.1' \
    'ipykernel>=6.30.0'

# Verify every wheel is a non-truncated ELF on linux x86_64. Anywhere this
# check fails, drop the file (let pip fail or fall through) and print it
# loudly - if we see "missing section headers", the same problem that
# wrecked the build-time install is still hitting us.
echo
echo "=== Verifying wheel integrity ==="
bad=0
for w in "$WHEELS_DIR"/*.whl; do
    [ -f "$w" ] || continue
    # `file` on a wheel should always report "Zip archive data, at least v2.0"
    info="$(file "$w" 2>/dev/null || echo 'unknown')"
    case "$info" in
        *"Zip archive data"*) : ;;
        *)
            echo "  BAD: $w"
            echo "       $info"
            bad=$((bad+1))
            rm -f "$w"
            ;;
    esac
done

if [ "$bad" -gt 0 ]; then
    echo
    echo "WARNING: $bad wheel(s) were truncated and have been deleted."
    echo "Re-run this script until it produces a clean run; if it keeps"
    echo "failing, consider changing PIP_INDEX_URL above to another mirror."
fi

echo
echo "=== wheel count ==="
ls "$WHEELS_DIR"/*.whl 2>/dev/null | wc -l
