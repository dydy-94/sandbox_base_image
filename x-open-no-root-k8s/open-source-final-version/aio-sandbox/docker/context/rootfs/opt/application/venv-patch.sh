#!/bin/bash
# entrypoint pre-step: ensure /opt/server-venv is healthy at container start.
#
# Why this exists:
#   The build's pip install passes (see Dockerfile §11) often land with
#   `missing section headers` truncated .so files for a handful of large
#   C-extensions (pydantic_core, uvloop, watchfiles, lxml, yaml, httptools,
#   pyzmq). At import time Python's dlopen fails or, worse, SIGSEGVs the
#   whole interpreter before any traceback can be printed.
#
# Even with the pre-staged wheels cache populated by prepare-wheels.sh on
# the host, the build-time copy is a one-shot — any subsequent rebuild of
# the same layer that the upstream maintainers push, or any live-patch
# release, can land a broken .so again. So at every container start we:
#
#   1) sweep broken .so in /opt/server-venv (delete them)
#   2) reinstall the C-extension packages we know to be problematic
#   3) ensure 'loop': 'asyncio' on uvicorn (otherwise SIGSEGV when import
#      uvloop fails)
#
# Doing this here — at container start, not at build time — means even
# if the build delivers a partially broken image, the user's first
# `docker run` is the one that fixes things. Worst case: --no-network
# mode fails reinstall, and python-server stays down; the user can
# `docker exec` and rerun the script by hand.
#
# This script is idempotent and exits 0 on success even if `pip install`
# is a no-op (everything already healthy).

set +e

VE=/opt/server-venv
WHEELS=/opt/wheels

# 1) sweep broken .so (any .so whose `file` reports `missing section headers`
#    or `can't read elf section`).
echo "[venv-patch] sweep broken .so in $VE"
find "$VE" -name '*.so' -print0 \
  | xargs -0 -I{} bash -c '
        f="$1"
        info="$(file "$f" 2>/dev/null)"
        if echo "$info" | grep -qE "missing section headers|can'"'"'t read elf section"; then
            echo "$f"
        fi
    ' _ {} > /tmp/broken_sos.txt
n=$(wc -l < /tmp/broken_sos.txt)
echo "[venv-patch]   broken: $n"
if [ "$n" -gt 0 ]; then
    head -40 /tmp/broken_sos.txt
    xargs -r -a /tmp/broken_sos.txt rm -f
fi

# 2) reinstall critical C-extensions whose pure-Python fallback isn't enough.
#    Pin exact versions we know are healthy on the CN mirror.
PKGS=(
    'typing_extensions>=4.13'
    'pydantic-core==2.46.4'
    'watchfiles>=1.0.0'
    'uvloop>=0.21.0'
    'lxml>=6.0.0'
    'pyyaml>=6.0.0'
    'httptools>=0.6.0'
    'anyio>=4.6.0'
    'pyzmq>=26.0.0'
    'jupyter-client>=8.6.3'
    'terminado>=0.18.1'
)

if [ -d "$WHEELS" ] && [ -n "$(ls -A "$WHEELS" 2>/dev/null)" ]; then
    echo "[venv-patch] pip install --no-index --find-links $WHEELS"
    # Install one package at a time, with --ignore-installed (NOT
    # --force-reinstall, which triggers a buggy pip file-move that
    # fails on Windows docker-desktop / overlayfs with
    # OSError Errno 22). If a wheel is missing or bad in $WHEELS, fall
    # back to network so a partial cache doesn't leave the venv broken.
    any_rc=""
    for p in "${PKGS[@]}"; do
        case "$p" in
            *pydantic-core*|*uvloop*|*watchfiles*|*lxml*|*pyyaml*|*httptools*|*pyzmq*)
                always=1 ;;
            *)
                always=0 ;;
        esac
        if [ "$always" = "1" ]; then
            # `--force-reinstall` is required to overwrite any truncated .so
            # files (pydantic_core, uvloop, watchfiles, etc.) — pip's
            # "already satisfied" check only looks at METADATA, not the
            # .so on disk. We avoid the Windows docker-desktop overlayfs
            # Errno 22 bug by using `--no-deps` so pip does not try to
            # move typing_extensions.py (which is what triggered it before).
            LOG=$("$VE/bin/pip" install --no-cache-dir --quiet --no-index \
                                      --find-links "$WHEELS" \
                                      --ignore-installed --force-reinstall --no-deps \
                                      "$p" 2>&1 | tail -5)
            rc=$?
            if [ "$rc" != "0" ]; then
                any_rc="1"
                if echo "$LOG" | grep -qE "Bad magic number|No matching distribution"; then
                    echo "[venv-patch]   $p: cache broken/missing — falling back to network"
                    OUT=$("$VE/bin/pip" install --no-cache-dir --quiet \
                                                --index-url "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
                                                --extra-index-url https://pypi.org/simple \
                                                --ignore-installed --force-reinstall --no-deps \
                                                "$p" 2>&1 | tail -5)
                    rcc=$?
                    [ "$rcc" != "0" ] && echo "[venv-patch]     network fallback rc=$rcc too — giving up on $p"
                else
                    echo "[venv-patch]   $p install rc=$rc: $LOG"
                fi
            fi

            # Belt-and-braces: if the module can't be imported (e.g.
            # anyio's __init__.py is 0-byte due to a prior --force-reinstall
            # on Windows-docker overlayfs), still try the network reinstall
            # unconditionally — many C-ext packages are at high risk of
            # this race, and dropping one network round-trip at startup
            # (on a container that boots maybe once per day) is worth it.
            pkgname=$(echo "$p" | sed -E 's/[<>=!~].*//' | tr -d ' ')
            # Strip trailing version decorators like ==2.46.4 or >=1.0.0
            # Then map common dist name -> import name (most are 1:1).
            case "$pkgname" in
                pydantic-core) pkgname=pydantic_core ;;
                jupyter-client) pkgname=jupyter_client ;;
                pyyaml) pkgname=yaml ;;
            esac
            if ! "$VE/bin/python" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$pkgname') is not None else 1)" 2>/dev/null; then
                echo "[venv-patch]   $p: import '$pkgname' not found — final network reinstall"
                "$VE/bin/pip" install --no-cache-dir --quiet --ignore-installed --force-reinstall \
                                      --index-url "${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" \
                                      --extra-index-url https://pypi.org/simple \
                                      "$p" 2>&1 | tail -3
            fi
        fi
    done
    if [ -z "$any_rc" ]; then
        echo "[venv-patch] reinstall OK"
    else
        echo "[venv-patch] pip partial — pure-Python fallbacks will cover skipped packages"
    fi
else
    echo "[venv-patch] no /opt/wheels cache; pip install will fall back to network"
    "$VE/bin/pip" install --no-cache-dir --quiet "${PKGS[@]}" 2>&1 | tail -5
fi

# 3) safe-by-default: force asyncio loop. uvloop is fast but its .so is
#    one of the most-fragmented wheels across CN mirrors; for a sandbox
#    whose workload is mixed I/O, the perf hit is negligible compared
#    to import-time SIGSEGVs. This is idempotent.
CLI=$VE/lib/python3.14/site-packages/app/cli.py
if [ -f "$CLI" ]; then
    if grep -q "'loop': 'uvloop'" "$CLI"; then
        sed -i "s/'loop': 'uvloop'/'loop': 'asyncio'/" "$CLI"
        echo "[venv-patch] cli.py loop=uvloop → asyncio"
    else
        echo "[venv-patch] cli.py loop already safe"
    fi
fi

echo "[venv-patch] done"
exit 0
