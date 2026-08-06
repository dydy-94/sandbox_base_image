#!/bin/bash
# Post-install fix: detect packages whose .so files are missing (pip
# silently dropped them due to docker-desktop overlayfs Errno 22), and
# re-extract the wheels via the tmp+move trick in /opt/application/sweep-so.py.
#
# This is the "real" fix that we can't express in Dockerfile RUN:
# wheel extraction that succeeds under overlay normal reads but fails
# under overlay rename. We work around the rename by extracting each
# wheel entry to a /tmp file first, then `shutil.move`-ing the tmp
# path to the real destination. The tmp path is on real tmpfs (not
# overlay), so it works.
#
# Then we chown the re-extracted files to the x user, since
# supervisor runs python-server as `x` not as root.

set -e
VE=/opt/server-venv
SITE=$VE/lib/python3.14/site-packages
PYE=$VE/bin/python
INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
DEST=/tmp/wheels-fresh

# Packages whose C-extensions we *know* get truncated.  Each entry is
# the wheel-filename pattern; we let pip download the right one and
# then extract via sweep-so.py.
PKGS="pydantic-core uvloop httptools lxml watchfiles pyzmq"

mkdir -p "$DEST" "$SITE"

# Step 1: scan for missing .so per package.
needs_fix=""
for pkg in $PKGS; do
    # python distribution name to import name differs sometimes
    pyname="$pkg"
    case "$pkg" in
        pydantic-core) pyname=pydantic_core ;;
        jupyter-client) pyname=jupyter_client ;;
    esac
    pkgdir="$SITE/$pyname"
    # Trigger a re-install if EITHER:
    #   - the package directory doesn't exist at all (pkgdir missing)
    #   - there are 0 .so files
    #   - the __init__.py is 0 bytes (Python sees a namespace package;
    #     C-extension symbols won't propagate, e.g. httptools.HttpRequestParser).
    # We deliberately check `_unconditionally_` and don't bail on missing
    # dir, so a deleted-or-never-installed package also gets re-installed.
    if [ ! -d "$pkgdir" ]; then
        echo "[post-inst] $pyname: package directory missing — needs reinstall"
        needs_fix="$needs_fix $pkg"
        continue
    fi
    n_so=$(find "$pkgdir" -name '*.so' 2>/dev/null | wc -l)
    empty_init=$(find "$pkgdir" -name __init__.py -size 0 2>/dev/null | wc -l)
    if [ "$n_so" = "0" ]; then
        echo "[post-inst] $pyname: 0 .so files — needs reinstall"
        needs_fix="$needs_fix $pkg"
    elif [ "$empty_init" -gt 0 ]; then
        echo "[post-inst] $pyname: has 0-byte __init__.py — needs reinstall"
        needs_fix="$needs_fix $pkg"
    fi
done

if [ -z "$needs_fix" ]; then
    echo "[post-inst] all .so files present — nothing to do"
    exit 0
fi

# Step 2: download wheels for the broken packages to /tmp.
echo "[post-inst] downloading fresh wheels for:$needs_fix"
for pkg in $needs_fix; do
    "$PYE" -m pip download --no-deps --dest "$DEST" \
        --index-url "$INDEX" \
        --extra-index-url https://pypi.org/simple \
        --prefer-binary \
        "$pkg" 2>&1 | tail -3
done

# Step 3: for each fresh wheel, nuke the broken package dir + extract
# back via the tmp+move trick. We delete the package's dist-info too,
# so pip's "already satisfied" check later doesn't short-circuit on
# stale metadata.
echo "[post-inst] re-extracting .so files"
for whl in "$DEST"/*.whl; do
    [ -e "$whl" ] || continue
    pkg=$(basename "$whl" | sed -E 's/-[0-9]+.*//')
    # delete the broken module directory + its dist-info
    rm -rf "$SITE/$pkg" "${pkg}"-*.dist-info
    # also delete the broken module's pyc cache (we don't want stale bytecode
    # referring to the broken .so to win over our new install).
    find "$SITE/$pkg" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
    echo "  extracting $whl → $SITE"
    "$PYE" /opt/application/sweep-so.py "$whl" "$SITE" 2>&1 | tail -5
    # chown so supervisor's `x` user can read; we just keep `x`'s gid
    if id x >/dev/null 2>&1; then
        chown -R 1000:1000 "$SITE/$pkg" 2>/dev/null || true
    fi
done

# Step 4: nuke the entire venv __pycache__ so Python re-imports the
# new .so files cleanly. Without this, the cached bytecode pins
# Python to the *old* (broken) symbols.
echo "[post-inst] clearing __pycache__"
find "$SITE" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true

# Step 5: supervisor runs python-server as `x`, not root, so we must
# chown everything we just extracted to 1000:1000. (Without this,
# uvicorn hits PermissionError on `from app.services.jupyter import
# JupyterService` because zmq/__init__.py is owned by root and
# mode 600.)  We only do this when a fix was actually triggered so
# that fast re-runs on healthy images don't churn the perms.
if id x >/dev/null 2>&1 && [ -n "$needs_fix" ]; then
    echo "[post-inst] chown -R 1000:1000 $SITE"
    chown -R 1000:1000 "$SITE" 2>/dev/null || true
fi

echo "[post-inst] done"
