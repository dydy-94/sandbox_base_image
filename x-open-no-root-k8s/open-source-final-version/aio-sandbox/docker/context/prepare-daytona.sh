#!/bin/bash
# prepare-daytona.sh — pre-stage the daytona daemon + computer-use plugin
# binaries that the Dockerfile COPY's into /usr/local/bin/ and
# /usr/local/lib/daytona-computer-use/.
#
# Run this script ONCE on the build host, BEFORE running `docker build`.
#
# Both binaries are pulled from the daytona open-source release on GitHub.
# The Dockerfile does NOT fall back to a network install if these files are
# missing, so this script is required for a successful offline build.
#
# Layout produced:
#   docker/context/bin/daytona
#   docker/context/dist/libs/computer-use-amd64

set -euo pipefail

CTX="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$CTX/bin"
DIST_DIR="$CTX/dist/libs"
mkdir -p "$BIN_DIR" "$DIST_DIR"

# Default version. daytona is a small Go binary; pin the release that the
# open-source aio-sandbox builds against (v0.x.y at the time of this final
# image). Override with DAYTONA_VERSION env if you need a newer build.
DAYTONA_VERSION="${DAYTONA_VERSION:-0.20.0}"
MIRRORS=(
    "https://github.com/daytonaio/daytona/releases/download/v${DAYTONA_VERSION}"
    "https://npmmirror.com/mirrors/daytona/v${DAYTONA_VERSION}"
)

# ---- 1. daytona daemon -----------------------------------------------------
echo "=== daytona daemon (v${DAYTONA_VERSION}) ==="
DAYTONA_BIN="$BIN_DIR/daytona"
if [ ! -f "$DAYTONA_BIN" ] || [ ! -s "$DAYTONA_BIN" ]; then
    ok=0
    for url in \
        "${MIRRORS[0]}/daytona-linux-amd64" \
        "${MIRRORS[1]}/daytona-linux-amd64"
    do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 180 --retry 2 \
                    -o "$DAYTONA_BIN" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$DAYTONA_BIN" 2>/dev/null || stat -f %z "$DAYTONA_BIN")" -gt 5000000 ]; then
            chmod +x "$DAYTONA_BIN"
            echo "  ok ($(du -h "$DAYTONA_BIN" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$DAYTONA_BIN"
    done
    if [ "$ok" -ne 1 ]; then
        echo "ERROR: daytona download failed from all mirrors" >&2
        echo "       The Dockerfile does not have a network fallback for this." >&2
        echo "       Please place a 'daytona' ELF binary at $DAYTONA_BIN manually." >&2
        exit 1
    fi
else
    echo "  already present: $DAYTONA_BIN ($(du -h "$DAYTONA_BIN" | awk '{print $1}'))"
fi

# ---- 2. daytona-computer-use plugin (Linux x86_64) -----------------------
echo
echo "=== daytona-computer-use plugin ==="
COMPUTER_USE_BIN="$DIST_DIR/computer-use-amd64"
if [ ! -f "$COMPUTER_USE_BIN" ] || [ ! -s "$COMPUTER_USE_BIN" ]; then
    ok=0
    # The plugin is shipped as part of the daytona `computer-use` release
    # artifact on GitHub. If the URL has moved, fall back to a generic
    # npmmirror copy.
    for url in \
        "${MIRRORS[0]}/computer-use-linux-amd64" \
        "${MIRRORS[1]}/computer-use-linux-amd64" \
        "https://github.com/daytonaio/daytona/releases/download/v${DAYTONA_VERSION}/daytona-computer-use-linux-amd64"
    do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 180 --retry 2 \
                    -o "$COMPUTER_USE_BIN" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$COMPUTER_USE_BIN" 2>/dev/null || stat -f %z "$COMPUTER_USE_BIN")" -gt 1000000 ]; then
            chmod +x "$COMPUTER_USE_BIN"
            echo "  ok ($(du -h "$COMPUTER_USE_BIN" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$COMPUTER_USE_BIN"
    done
    if [ "$ok" -ne 1 ]; then
        echo "ERROR: computer-use-amd64 download failed from all mirrors" >&2
        echo "       Place the binary at $COMPUTER_USE_BIN manually." >&2
        exit 1
    fi
else
    echo "  already present: $COMPUTER_USE_BIN ($(du -h "$COMPUTER_USE_BIN" | awk '{print $1}'))"
fi

# ---- summary ---------------------------------------------------------------
echo
echo "=== Summary ==="
printf '  %-30s %s\n' "bin/daytona" "$(du -h "$DAYTONA_BIN" 2>/dev/null | awk '{print $1}')"
printf '  %-30s %s\n' "dist/libs/computer-use-amd64" "$(du -h "$COMPUTER_USE_BIN" 2>/dev/null | awk '{print $1}')"
echo "Done."