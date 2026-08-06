#!/bin/bash
# prepare-rust.sh — pre-stage the rust toolchain + agent-browser source
# for offline docker build.
#
# Run this script ONCE on the build host, BEFORE running `docker build`.
#
# What it does (in order):
#   1. Download the `rustup-init` binary matching the host arch, save to
#      docker/context/rustup-pre/ for the Dockerfile to COPY directly.
#   2. Clone agent-browser (and pin its rev) into
#      docker/context/cargo-vendored/agent-browser/. The Dockerfile's
#      `cargo install --offline` step then compiles from this local
#      checkout, with crates.io only touched as the LAST resort fallback.
#
# This is intentionally lighter than full `cargo vendor` (which would
# produce hundreds of binary .crate files in cargo-vendored/vendor/):
# `cargo install --offline` against a local source tree still uses a
# normal Cargo.lock-anchored registry, just resolving all crates by
# downloading them once ahead of time.
#
# Layout produced:
#   docker/context/rustup-pre/
#     rustup-init-<target>              ← single binary
#   docker/context/cargo-vendored/
#     agent-browser/                    ← git checkout (Cargo.lock included)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RUSTUP_PRE="$ROOT/rustup-pre"
CARGO_PRE="$ROOT/cargo-vendored"
mkdir -p "$RUSTUP_PRE" "$CARGO_PRE"
# `.keep` so the Dockerfile's COPY doesn't fail if a particular prep
# step wasn't needed.
touch "$RUSTUP_PRE/.keep" "$CARGO_PRE/.keep"

# Pick the right rustup-init binary per arch. `uname -m` returns x86_64
# on Windows msys/Git-Bash when the kernel is amd64; arm64 macs return
# arm64. If you are cross-building, override via RUSTARCH below.
RUSTARCH="${RUSTARCH:-}"
case "$RUSTARCH" in
    "") RUSTARCH="$(uname -m)" ;;
esac
case "$RUSTARCH" in
    x86_64|amd64)  RUSTBIN=rustup-init-x86_64-unknown-linux-gnu ;;
    aarch64|arm64) RUSTBIN=rustup-init-aarch64-unknown-linux-gnu ;;
    *) echo "ERROR: unknown arch $RUSTARCH" >&2; exit 1 ;;
esac

# === 1. rustup-init ===
echo "=== rustup-init ($RUSTBIN) ==="
RUSTUP_TARBALL="$RUSTUP_PRE/$RUSTBIN"
if [ -f "$RUSTUP_TARBALL" ] && [ -s "$RUSTUP_TARBALL" ]; then
    echo "Already present: $RUSTUP_TARBALL ($(du -h "$RUSTUP_TARBALL" | awk '{print $1}'))"
else
    # The CN mirrors no longer carry the per-arch rustup-init binary;
    # try upstream distribution paths in order.
    tried=""
    for url in \
        "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init" \
        "https://mirrors.ustc.edu.cn/rustup/static/dist/x86_64-unknown-linux-gnu/rustup-init"
    do
        echo "[download] $url"
        if curl -fSL --retry 3 --retry-delay 5 --connect-timeout 30 \
               "$url" -o "$RUSTUP_TARBALL" 2>/dev/null; then
            sz=$(stat -c %s "$RUSTUP_TARBALL" 2>/dev/null || stat -f %z "$RUSTUP_TARBALL")
            if [ "$sz" -gt 5000000 ]; then
                chmod +x "$RUSTUP_TARBALL"
                echo "Downloaded: $((sz/1024/1024)) MB"
                tried="ok"
                break
            else
                echo "[download] too small ($sz bytes), retrying" >&2
                rm -f "$RUSTUP_TARBALL"
            fi
        fi
    done
    if [ "$tried" != "ok" ]; then
        echo "WARN: could not fetch rustup-init from any mirror; build-time will fall back to network" >&2
    fi
fi

# === 2. agent-browser source ===
# Pin a specific git rev in case upstream master moves and breaks our
# build. The Dockerfile passes AGENT_BROWSER_VERSION as an ARG, but
# we still need a source tree on disk to compile from offline.
AGENT_BROWSER_VERSION="${AGENT_BROWSER_VERSION:-0.27.1}"
AGENT_DIR="$CARGO_PRE/agent-browser"
echo "=== agent-browser source (rev $AGENT_BROWSER_VERSION) ==="

if [ -d "$AGENT_DIR/.git" ]; then
    echo "Already checked out at $(cd "$AGENT_DIR" && git rev-parse HEAD)"
    echo "  (delete this dir to force re-clone)"
else
    if command -v git >/dev/null 2>&1; then
        # Upstream repo: vercel-labs/agent-browser (the historical
        # nicebyte fork was archived in 2025). Try the tag first, then
        # fall back to default branch if the tag doesn't exist.
        AGENT_REPOS=(
            "https://github.com/vercel-labs/agent-browser.git"
            "https://github.com/vercel-labs/agent-browser.git"
        )
        AGENT_REFS=(
            "v${AGENT_BROWSER_VERSION}"
            ""
        )
        ok=0
        for i in 0 1; do
            url="${AGENT_REPOS[$i]}"
            ref="${AGENT_REFS[$i]}"
            if [ -n "$ref" ]; then
                echo "[clone] $url (tag $ref)"
                if git clone --depth 1 --branch "$ref" "$url" "$AGENT_DIR" 2>&1 | tail -3; then
                    ok=1
                    break
                fi
            else
                echo "[clone] $url (default branch)"
                if git clone --depth 1 "$url" "$AGENT_DIR" 2>&1 | tail -3; then
                    ok=1
                    break
                fi
            fi
            echo "[clone] tag/branch failed, trying next" >&2
        done
        if [ "$ok" != "1" ]; then
            echo "WARN: agent-browser clone failed; build will try network"
        fi
    else
        echo "ERROR: no git on PATH; install git first" >&2
        exit 1
    fi
fi

# === 3. (optional) pre-fetch agent-browser's cargo deps ===
# `cargo fetch --manifest-path <Cargo.toml>` pulls every crate mentioned
# in the lockfile into the local cargo cache. This step is not strictly
# required (the build will do the same thing at first compile), but
# it surfaces any network failure here rather than at build time.
if command -v cargo >/dev/null 2>&1 && [ -f "$AGENT_DIR/Cargo.toml" ]; then
    echo "=== cargo fetch (pre-warm) ==="
    (cd "$AGENT_DIR" && cargo fetch 2>&1) | tail -5 \
        || echo "WARN: cargo fetch had errors; build-time cargo will retry"
fi

# === summary ===
echo "=== Summary ==="
[ -f "$RUSTUP_PRE/$RUSTBIN" ]      && printf '  %-40s %s\n' "$RUSTUP_PRE/$RUSTBIN" "$(du -h "$RUSTUP_PRE/$RUSTBIN" | awk '{print $1}')"
[ -d "$AGENT_DIR" ]                && printf '  %-40s %s\n' "$AGENT_DIR" "$(du -sh "$AGENT_DIR" | awk '{print $1}')"
echo "Done."
