#!/bin/bash
# prepare-fnm-node.sh — pre-stage the fnm binary + Node.js 22 tarball that
# Dockerfile.final §7 COPY's into the image (offline node install).
#
# Run this script ONCE on the build host, BEFORE running `docker build`.
#
# Layout produced:
#   docker/context/fnm-node/fnm-linux.zip
#   docker/context/fnm-node/node-v<ver>-linux-x64.tar.xz
#
# The Dockerfile falls back to network if these are missing, but having them
# makes the build fully offline for the node toolchain.

set -euo pipefail

CTX="$(cd "$(dirname "$0")" && pwd)"
DEST="$CTX/fnm-node"
mkdir -p "$DEST"

# Keep in sync with Dockerfile.final ARGs (FNM_VERSION / NODE22_VERSION).
FNM_VERSION="${FNM_VERSION:-1.39.0}"
NODE22_VERSION="${NODE22_VERSION:-22.22.3}"
NODE_ARCH="${NODE_ARCH:-x64}"   # override with arm64 for aarch64 builds

# ---- 1. fnm ---------------------------------------------------------------
echo "=== fnm v${FNM_VERSION} ==="
FNM_ZIP="$DEST/fnm-linux.zip"
if [ ! -f "$FNM_ZIP" ] || [ ! -s "$FNM_ZIP" ]; then
    ok=0
    for url in \
        "https://github.com/Schniz/fnm/releases/download/v${FNM_VERSION}/fnm-linux.zip" \
        "https://npmmirror.com/mirrors/fnm/v${FNM_VERSION}/fnm-linux.zip"
    do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 \
                    -o "$FNM_ZIP" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$FNM_ZIP" 2>/dev/null || stat -f %z "$FNM_ZIP")" -gt 1000000 ]; then
            echo "  ok ($(du -h "$FNM_ZIP" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$FNM_ZIP"
    done
    if [ "$ok" -ne 1 ]; then
        echo "ERROR: fnm download failed from all mirrors" >&2
        echo "       Place fnm-linux.zip at $FNM_ZIP manually." >&2
        exit 1
    fi
else
    echo "  already present: $FNM_ZIP ($(du -h "$FNM_ZIP" | awk '{print $1}'))"
fi

# ---- 2. node 22 tarball ---------------------------------------------------
echo
echo "=== node v${NODE22_VERSION} (linux-${NODE_ARCH}) ==="
NODE_TARBALL="$DEST/node-v${NODE22_VERSION}-linux-${NODE_ARCH}.tar.xz"
if [ ! -f "$NODE_TARBALL" ] || [ ! -s "$NODE_TARBALL" ]; then
    ok=0
    for url in \
        "https://cdn.npmmirror.com/binaries/node/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-${NODE_ARCH}.tar.xz" \
        "https://nodejs.org/dist/v${NODE22_VERSION}/node-v${NODE22_VERSION}-linux-${NODE_ARCH}.tar.xz"
    do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 600 --retry 2 \
                    -o "$NODE_TARBALL" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$NODE_TARBALL" 2>/dev/null || stat -f %z "$NODE_TARBALL")" -gt 10000000 ]; then
            echo "  ok ($(du -h "$NODE_TARBALL" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$NODE_TARBALL"
    done
    if [ "$ok" -ne 1 ]; then
        echo "ERROR: node tarball download failed from all mirrors" >&2
        echo "       Place it at $NODE_TARBALL manually." >&2
        exit 1
    fi
else
    echo "  already present: $NODE_TARBALL ($(du -h "$NODE_TARBALL" | awk '{print $1}'))"
fi

# ---- summary ---------------------------------------------------------------
echo
echo "=== Summary ==="
printf '  %-30s %s\n' "fnm-linux.zip" "$(du -h "$FNM_ZIP" | awk '{print $1}')"
printf '  %-30s %s\n' "$(basename "$NODE_TARBALL")" "$(du -h "$NODE_TARBALL" | awk '{print $1}')"
echo "Done."
