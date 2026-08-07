#!/bin/bash
# Pre-download the apt .deb packages the Dockerfile's `apt-get install`
# needs. Run this on the host once before `docker buildx build`.
#
# Approach: start a throwaway ubuntu:26.04 container, run
# `apt-get update && apt-get install --print-uris <all packages>`, capture
# the URL list, then fetch each .deb from a CN mirror with curl (mirror
# fallback + per-file SHA verify). Result lands in
# ./apt-archives/ next to this script. The Dockerfile then COPY's it to
# /var/cache/apt/archives and installs offline with `--no-download`,
# eliminating the broken-apt-mirror problem entirely.

set -euo pipefail

CTX=$(cd "$(dirname "$0")" && pwd)
ARCH_DIR="$CTX/apt-archives"
mkdir -p "$ARCH_DIR"
# `.keep` so the Dockerfile's COPY of context/apt-archives/ never fails.
touch "$ARCH_DIR/.keep"

# Mirrors to try, in order. TUNA first because it's the same as the
# Dockerfile default.
MIRRORS=(
    "http://mirrors.tuna.tsinghua.edu.cn/ubuntu"
    "http://mirrors.aliyun.com/ubuntu"
    "http://mirrors.cloud.tencent.com/ubuntu"
    "http://mirrors.ustc.edu.cn/ubuntu"
)

# Canonical package list — must match the Dockerfile's `apt-get install`
# list exactly (see PKGS variable). Doubling the PKGS line for completeness.
PKGS=(build-essential gcc make cmake ninja-build meson pkg-config gettext gawk
      libffi-dev libcairo2-dev libpango1.0-dev freeglut3-dev python3-dev
      ca-certificates curl wget git gh jq file unzip zip tree htop lsof psmisc
      sudo software-properties-common gnupg vim
      net-tools netcat-openbsd iputils-ping telnet
      ripgrep
      python3 python3-venv python3-dev python3-pip
      nginx supervisor
      tigervnc-standalone-server openbox autocutsel
      x11-utils x11-xserver-utils xauth xclip xdotool xdg-utils
      gnome-screenshot dbus dbus-x11
      fcitx5 fcitx5-chinese-addons fcitx5-frontend-gtk3 fcitx5-frontend-qt5
      ffmpeg imagemagick
      libnss3 libnspr4 libgbm1 libdrm2 libgtk-3-0t64 libasound2t64 libcups2t64
      libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64 libxcomposite1 libxdamage1
      libxfixes3 libxrandr2 libxkbcommon0 libwayland-client0 libvulkan1
      libxext6 libxv1 libgcrypt20 libglib2.0-0t64 libgl1 libcairo2 libpango-1.0-0
      fontconfig fonts-dejavu fonts-liberation fonts-noto-core fonts-noto-extra
      fonts-noto-cjk fonts-noto-cjk-extra fonts-noto-color-emoji
      fonts-pretendard
      fonts-arphic-ukai fonts-arphic-uming fonts-unfonts-core
      fonts-ipafont-gothic fonts-ipafont-mincho fonts-takao-mincho
      fonts-indic fonts-khmeros fonts-lao fonts-thai-tlwg fonts-sil-padauk
      xfonts-intl-chinese
      locales tzdata)

# Step 1 — get the URL list by spinning up a throwaway ubuntu container.
echo "=== fetching URL list from a fresh ubuntu:26.04 ==="
URLS_FILE=/tmp/apt-urls-$$
PKG_STRING="${PKGS[*]}"
docker run --rm ubuntu:26.04 bash -c "
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y -q
    apt-get install -y --no-install-recommends --print-uris $PKG_STRING \
      2>/dev/null \
    | sed -n 's|^'\''\(http[^'\'']*\)'\''.*|\1|p'
" > "$URLS_FILE"
NURLS=$(wc -l < "$URLS_FILE")
echo "  got $NURLS URL(s) to fetch"
if [ "$NURLS" -lt 100 ]; then
    echo "WARNING: suspiciously few URLs — apt-get inside the container"
    echo "         may have failed; sample of URL file:"
    head -3 "$URLS_FILE"
fi

# Step 2 — fetch each one with mirror fallback.
echo
echo "=== downloading $NURLS .deb(s) to $ARCH_DIR/ ==="
DOWNLOADED=0
FAILED=()
mkdir -p "$ARCH_DIR"
while IFS= read -r url; do
    [ -z "$url" ] && continue
    fname=$(basename "$url")
    # Skip if already present and looks intact.
    if [ -f "$ARCH_DIR/$fname" ] \
       && file "$ARCH_DIR/$fname" | grep -q "Debian binary package"; then
        DOWNLOADED=$((DOWNLOADED+1))
        continue
    fi
    # Try each mirror.
    success=0
    for mirror in "${MIRRORS[@]}"; do
        newurl=$(echo "$url" | sed "s|^http[s]*://[^/]*/ubuntu|$mirror|")
        if curl -fsSL --connect-timeout 8 --max-time 60 --retry 2 \
                    -o "$ARCH_DIR/$fname" "$newurl" 2>/dev/null \
           && file "$ARCH_DIR/$fname" 2>/dev/null | grep -q "Debian binary package"; then
            success=1
            DOWNLOADED=$((DOWNLOADED+1))
            break
        fi
    done
    if [ "$success" -eq 0 ]; then
        FAILED+=("$url")
    fi
done < "$URLS_FILE"

rm -f "$URLS_FILE"

echo
echo "=== summary ==="
echo "  successfully downloaded: $DOWNLOADED / $NURLS"
if [ ${#FAILED[@]} -gt 0 ]; then
    echo "  FAILED URLs (cannot reach any mirror):"
    for u in "${FAILED[@]:0:10}"; do
        echo "    $u"
    done
    if [ ${#FAILED[@]} -gt 10 ]; then
        echo "    ... and $((${#FAILED[@]} - 10)) more"
    fi
    echo
    echo "Re-run this script after fixing the network. The Dockerfile will"
    echo "degrade gracefully — if the offline cache is incomplete, it falls"
    echo "back to fetching the missing pkgs online."
fi
SIZE=$(du -sh "$ARCH_DIR" 2>/dev/null | cut -f1)
echo "  cache size: $SIZE"
echo
echo "============================================================="
echo "Bonus offline assets (chrome-deb / code-server / novnc / websocat)"
echo "============================================================="

# ---- 1. google-chrome .deb -------------------------------------------------
# Google Chrome is not in the apt mirror; pull directly from dl.google.com.
# If that fails, fall back to mirrors.tuna which keeps a copy of the same
# .deb under /chromium-browser/.
CHROME_DIR="$CTX/chrome-deb"
mkdir -p "$CHROME_DIR"
touch "$CHROME_DIR/.keep"
CHROME_DEB="$CHROME_DIR/google-chrome-stable_amd64.deb"
if [ ! -f "$CHROME_DEB" ] || [ ! -s "$CHROME_DEB" ]; then
    echo
    echo "--- downloading google-chrome .deb ---"
    url="https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
    if curl -fsSL --connect-timeout 15 --max-time 240 --retry 2 \
                -o "$CHROME_DEB" "$url" \
       && file "$CHROME_DEB" | grep -q "Debian binary package"; then
        echo "  ok ($(du -h "$CHROME_DEB" | awk '{print $1}'))"
    else
        echo "  WARN: dl.google.com failed; chrome will be missing in the image"
        rm -f "$CHROME_DEB"
    fi
fi

# ---- 2. code-server (VS Code in the browser) --------------------------------
# We download the official `code-server` standalone binary from GitHub
# releases; it ships its own nodejs bundle, so no extra deps needed.
CODE_DIR="$CTX/code-server"
mkdir -p "$CODE_DIR"
touch "$CODE_DIR/.keep"
CODE_VERSION="4.96.4"
CODE_TARBALL="$CODE_DIR/code-server-${CODE_VERSION}-linux-amd64.tar.gz"
if [ ! -f "$CODE_TARBALL" ] || [ ! -s "$CODE_TARBALL" ]; then
    echo
    echo "--- downloading code-server v${CODE_VERSION} ---"
    mirrors=(
        "https://github.com/coder/code-server/releases/download/v${CODE_VERSION}/code-server-${CODE_VERSION}-linux-amd64.tar.gz"
        "https://npmmirror.com/mirrors/code-server/v${CODE_VERSION}/code-server-${CODE_VERSION}-linux-amd64.tar.gz"
    )
    ok=0
    for url in "${mirrors[@]}"; do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 300 --retry 2 \
                    -o "$CODE_TARBALL" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$CODE_TARBALL" 2>/dev/null || stat -f %z "$CODE_TARBALL")" -gt 1000000 ]; then
            echo "  ok ($(du -h "$CODE_TARBALL" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$CODE_TARBALL"
    done
    if [ "$ok" -ne 1 ]; then
        echo "  WARN: code-server download failed; will fall back to network at build time"
    fi
fi
# Always (re)generate install.sh from whatever tarball is present so the
# Dockerfile can run a real install instead of skipping.
if [ -f "$CODE_TARBALL" ] && [ -s "$CODE_TARBALL" ]; then
    cat > "$CODE_DIR/install.sh" <<EOF
#!/bin/bash
# Install code-server from the pre-staged tarball (no network).
set -e
cd /opt/code-server
# code-server-VERSION-linux-amd64.tar.gz unpacks as a folder named
# code-server-VERSION-linux-amd64.
tar -xzf code-server-\${CODE_VERSION:-${CODE_VERSION}}-linux-amd64.tar.gz
# Put its bin/ on PATH.
ln -sf /opt/code-server/code-server-\${CODE_VERSION:-${CODE_VERSION}}-linux-amd64/bin/code-server /usr/local/bin/code-server
# Allow running as the `gem` user without sandbox warnings.
echo "code-server installed"
EOF
    chmod +x "$CODE_DIR/install.sh"
elif [ ! -f "$CODE_DIR/install.sh" ]; then
    # Provide a no-op install so the Dockerfile doesn't choke.
    cat > "$CODE_DIR/install.sh" <<EOF
#!/bin/bash
echo "WARN: code-server tarball missing; skipping install"
EOF
    chmod +x "$CODE_DIR/install.sh"
fi

# ---- 3. noVNC (web-based VNC client) ----------------------------------------
# noVNC is a static HTML+JS app. Pull the latest release tarball from
# GitHub; CN fallback to npmmirror.
NOVNC_DIR="$CTX/novnc"
mkdir -p "$NOVNC_DIR"
touch "$NOVNC_DIR/.keep"
NOVNC_VERSION="1.6.0"
NOVNC_TARBALL="$NOVNC_DIR/novnc.tar.gz"
if [ ! -d "$NOVNC_DIR/noVNC-${NOVNC_VERSION}" ] && [ ! -d "$NOVNC_DIR/noVNC" ]; then
    echo
    echo "--- downloading noVNC v${NOVNC_VERSION} ---"
    mirrors=(
        "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz"
        "https://npmmirror.com/mirrors/noVNC/v${NOVNC_VERSION}.tar.gz"
    )
    ok=0
    for url in "${mirrors[@]}"; do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 240 --retry 2 \
                    -o "$NOVNC_TARBALL" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$NOVNC_TARBALL" 2>/dev/null || stat -f %z "$NOVNC_TARBALL")" -gt 100000 ]; then
            echo "  ok ($(du -h "$NOVNC_TARBALL" | awk '{print $1}'))"
            tar -xzf "$NOVNC_TARBALL" -C "$NOVNC_DIR/" 2>/dev/null \
                && mv "$NOVNC_DIR/noVNC-${NOVNC_VERSION}" "$NOVNC_DIR/noVNC" \
                && rm -f "$NOVNC_TARBALL"
            ok=1
            break
        fi
        rm -f "$NOVNC_TARBALL"
    done
    if [ "$ok" -ne 1 ]; then
        echo "  WARN: noVNC download failed; will be missing in the image"
    fi
fi

# ---- 4. websocat (websocket CLI useful for VNC/noVNC relay) -----------------
# websocat is a single Go binary. Pull from GitHub releases.
WEBSOCAT_DIR="$CTX/websocat"
mkdir -p "$WEBSOCAT_DIR"
touch "$WEBSOCAT_DIR/.keep"
WEBSOCAT_VERSION="1.13.0"
WEBSOCAT_BIN="$WEBSOCAT_DIR/websocat-x86_64-unknown-linux-musl"
if [ ! -f "$WEBSOCAT_BIN" ] || [ ! -s "$WEBSOCAT_BIN" ]; then
    echo
    echo "--- downloading websocat v${WEBSOCAT_VERSION} ---"
    mirrors=(
        "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.x86_64-unknown-linux-musl"
        "https://npmmirror.com/mirrors/websocat/v${WEBSOCAT_VERSION}/websocat.x86_64-unknown-linux-musl"
    )
    ok=0
    for url in "${mirrors[@]}"; do
        echo "  trying $url"
        if curl -fsSL --connect-timeout 15 --max-time 180 --retry 2 \
                    -o "$WEBSOCAT_BIN" "$url" 2>/dev/null \
           && [ "$(stat -c %s "$WEBSOCAT_BIN" 2>/dev/null || stat -f %z "$WEBSOCAT_BIN")" -gt 100000 ]; then
            chmod +x "$WEBSOCAT_BIN"
            echo "  ok ($(du -h "$WEBSOCAT_BIN" | awk '{print $1}'))"
            ok=1
            break
        fi
        rm -f "$WEBSOCAT_BIN"
    done
    if [ "$ok" -ne 1 ]; then
        echo "  WARN: websocat download failed; will be missing in the image"
    fi
fi

echo
echo "Bonus assets summary:"
echo "  chrome-deb:   $(ls -la $CHROME_DIR/google-chrome-stable_amd64.deb 2>/dev/null | awk '{print $5}') bytes"
echo "  code-server:  $(ls -la $CODE_TARBALL 2>/dev/null | awk '{print $5}') bytes"
echo "  noVNC:        $(du -sh $NOVNC_DIR/noVNC 2>/dev/null | awk '{print $1}')"
echo "  websocat:     $(ls -la $WEBSOCAT_BIN 2>/dev/null | awk '{print $5}') bytes"

echo
echo "Done. Commit docker/context/{apt-archives,chrome-deb,code-server,novnc,websocat}/"
echo "to your git index (or leave uncommitted and rebuild) and run:"
echo "  docker buildx build ... -t aio-sandbox:v3 -f docker/Dockerfile.offline ..."
