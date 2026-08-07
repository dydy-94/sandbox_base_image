#!/bin/bash
# Pre-download npm tarballs the Dockerfile needs as offline .tgz files.
#
# Run this script ONCE on the build host, BEFORE running `docker build`.
# It uses `npm pack` against the CN mirror (registry.npmmirror.com), which
# downloads each package's tarball into a directory we COPY into the image.
#
# After this, `docker build` runs `npm install --prefer-offline /tmp/npm-tgz/*.tgz`
# and never has to reach out to the npm registry again. Truncated tarballs
# from the live CN mirror can no longer block the build.
#
# Layout produced:
#   docker/context/npm-tgz/
#     aio/                *.tgz   (aio-sandbox-cli's dev deps)
#     static-assets/      *.tgz   (static-assets build deps)
#     bun/                *.tgz   (bun tarball)
#     repl/               *.tgz   (repl-servers/nodejs deps, Dockerfile §7b)
#     global/             *.tgz   (Dockerfile §15 global npm packages:
#                                   claude-code, pm2, mcp2rest, ...)
#
# Each Dockerfile COPY has a path it expects — see Dockerfile.final.

set -euo pipefail

# Where the .tgz files land inside the build context.
ROOT="$(cd "$(dirname "$0")" && pwd)/npm-tgz"
mkdir -p "$ROOT"/{aio,static-assets,bun,repl,global}
# `.keep` files ensure the dirs exist if no tgz landed there, so the
# Dockerfile's `COPY context/npm-tgz/<bundle>/ /tmp/npm-tgz/` line
# never fails with "source path doesn't exist".
touch "$ROOT/aio/.keep" "$ROOT/static-assets/.keep" "$ROOT/bun/.keep" \
      "$ROOT/repl/.keep" "$ROOT/global/.keep"

# On Windows / Git-bash, MSYS's path translation can prevent bash from
# finding `node.exe` even when its dir is on PATH (because the path
# contains a space). The `npm` wrapper script sidesteps that; `node`
# does not.
#
# Workaround: have npm itself tell us the actual location of node.
# Inside Git-bash, `npm exec` / `npm config get` invokes the Windows
# `npm`, which sets up its own PATH and remembers the node-bin dir.
# Specifically, `npm root -g` always works (we'll see Windows C:\…
# paths).
if ! command -v npm >/dev/null 2>&1; then
    echo "ERROR: no npm on PATH; install Node.js 20+ first." >&2
    exit 1
fi
if ! command -v node >/dev/null 2>&1; then
    # Find where npm thinks node is. Run `npm config get prefix` —
    # this returns the path where npm's bin/ lives, which is also
    # where node.exe lives for normal Windows installs.
    NPM_PREFIX="$(npm config get prefix 2>/dev/null || true)"
    if [ -n "$NPM_PREFIX" ]; then
        # npm prefix is Windows-style; convert to MSYS /c/ form.
        case "$NPM_PREFIX" in
            C:\\*) npm_prefix_msys="/c/${NPM_PREFIX:3}" ;;
            D:\\*) npm_prefix_msys="/d/${NPM_PREFIX:3}" ;;
            *) npm_prefix_msys="$NPM_PREFIX" ;;
        esac
        npm_prefix_msys="$(printf '%s' "$npm_prefix_msys" | tr '\\' '/')"
        # Use `test -e` to actually check that node.exe exists there.
        # We avoid bash's `-x` because it sometimes fails on Windows
        # .exe files inside MSYS — `-e` is more reliable.
        if [ -e "$npm_prefix_msys/node.exe" ]; then
            export PATH="$npm_prefix_msys:$PATH"
        fi
        # As a belt-and-braces, also try invoking via `npm exec` which
        # spawns node by absolute path internally.
    fi
    if ! command -v node >/dev/null 2>&1; then
        # Last-resort fallback: try common Windows install locations
        # using `-e` tests (which work even when `-x` fails). Each
        # candidate uses the MSYS /c/... form so MSYS path translation
        # doesn't get a chance to drop the path.
        for candidate in \
            "/c/Program Files/nodejs/node.exe" \
            "/c/Program Files (x86)/nodejs/node.exe"
        do
            if [ -e "$candidate" ]; then
                dir=$(dirname "$candidate")
                export PATH="$dir:$PATH"
                break
            fi
        done
    fi
    if ! command -v node >/dev/null 2>&1; then
        echo "ERROR: no node on PATH; install Node.js 20+ first." >&2
        exit 1
    fi
fi

# Use the CN mirror. The build expects NPM_REGISTRY to be set; we read it
# from the env so the script picks up whatever the user is using.
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

# `npm pack` resolves everything from a directory/package.json and writes
# one .tgz per dep into the cwd. We run it in a tmp dir with the lockfile
# so the produced set is reproducible.
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# Helper: run npm pack for a package.json directory, copy all .tgz into
# the target dir.
pack_into() {
    local src="$1"                     # path to a directory that has package.json
    local dst="$2"                     # dest dir to receive the *.tgz files
    local package_json="$src/package.json"
    if [ ! -f "$package_json" ]; then
        echo "ERROR: $package_json not found" >&2
        return 1
    fi
    local lock="$src/package-lock.json"
    [ -f "$lock" ] || lock=""

    echo "=== npm pack for $src ==="
    local pkg_work="$WORK/$(basename "$src")"
    mkdir -p "$pkg_work"
    cp "$package_json" "$pkg_work/"
    [ -n "$lock" ] && cp "$lock" "$pkg_work/"

    # Set registry only for this `npm` invocation — does NOT touch the user's
    # global npm config.
    (
        cd "$pkg_work"
        echo "[pack] resolving from $NPM_REGISTRY"
        # `npm install --omit=dev --json --prefix <tmp>` would also pull deps,
        # but `npm pack` is the right primitive: it produces .tgz files for
        # every dep that the package can install.
        # We work around no-network installs (none here — the user has set
        # the registry) by running `npm pack <dep>` per dep listed in the
        # top-level package.json. This way we tolerate direct registry
        # 502s without aborting the whole run.
        if [ -f "package-lock.json" ]; then
            echo "[pack] using package-lock.json"
            # Iterate over each entry. npm lockfileVersion 3+ omits the
        # `name` field on most entries — fall back to the key path.
        node -e '
const lock=require("./package-lock.json");
const pkgs=lock.packages||{};
const seen=new Set();
const out=[];
for (const k of Object.keys(pkgs)) {
    if (k === "") continue;
    const p = pkgs[k];
    if (!p) continue;
    const name = p.name || k.replace(/^node_modules\//, "").replace(/\/+$/, "");
    const version = p.version;
    if (!name || !version) continue;
    const key = name+"@"+version;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(key);
}
console.log(out.sort().join("\n"));
' > "$WORK/deps.list"
            # pack each name@version
            while IFS= read -r line; do
                [ -z "$line" ] && continue
                echo "[pack] $line"
                # Try with --registry, fall back silently on 502.
                npm pack --registry "$NPM_REGISTRY" --pack-destination "$dst" "$line" \
                    >/dev/null 2>&1 \
                || npm pack --registry https://registry.npmjs.org --pack-destination "$dst" "$line" \
                    >/dev/null 2>&1 \
                || echo "[pack] WARN: $line failed; build-time install will retry"
            done < "$WORK/deps.list"
        else
            # No lock file. Iterate over declared deps (top-level only).
            for dep in $(node -e '
const pkg=require("./package.json");
const deps={...pkg.dependencies||{}, ...pkg.devDependencies||{}};
for (const k of Object.keys(deps)) console.log(k+"@"+deps[k]);
'); do
                echo "[pack] $dep"
                npm pack --registry "$NPM_REGISTRY" --pack-destination "$dst" "$dep" \
                    >/dev/null 2>&1 \
                || npm pack --registry https://registry.npmjs.org --pack-destination "$dst" "$dep" \
                    >/dev/null 2>&1 \
                || echo "[pack] WARN: $dep failed"
            done
        fi
    )
}

# === aio bundle ===
pack_into "$(cd "$(dirname "$0")" && pwd)/aio" "$ROOT/aio"

# === static-assets bundle ===
pack_into "$(cd "$(dirname "$0")" && pwd)/static-assets" "$ROOT/static-assets"

# === bun global tarball ===
echo "=== bun global tarball ==="
BUN_VERSION="${BUN_VERSION:-1.3.14}"
if ! ls "$ROOT/bun"/*.tgz >/dev/null 2>&1; then
    if npm pack --registry "$NPM_REGISTRY" --pack-destination "$ROOT/bun" "bun@${BUN_VERSION}" \
        >/dev/null 2>&1; then
        echo "bun tarball downloaded ($(ls -la "$ROOT/bun" | grep -c '\.tgz') files)"
    else
        echo "WARN: failed to fetch bun@${BUN_VERSION}; build will try network"
    fi
fi

# === repl-servers/nodejs bundle (Dockerfile §7b) ===
pack_into "$(cd "$(dirname "$0")" && pwd)/repl-servers/nodejs" "$ROOT/repl"

# === global npm packages bundle (Dockerfile §15) ===
# claude-code, pm2, mcp2rest, chrome-devtools-mcp, ... are `npm install -g`
# in the image. Synthesize a package.json + full lockfile, then pack every
# resolved dep so the image's
#   `npm install -g --prefer-offline /tmp/npm-tgz/global/*.tgz`
# never touches the registry. Keep versions in sync with Dockerfile.final §15.
GLOBAL_SRC="$WORK/global-src"
mkdir -p "$GLOBAL_SRC"
cat > "$GLOBAL_SRC/package.json" <<'GLOBAL_EOF'
{
  "name": "aio-sandbox-global-pkgs",
  "version": "1.0.0",
  "private": true,
  "dependencies": {
    "tsx": "^4.19.2",
    "typescript": "^5.0.0",
    "mcp2rest": "*",
    "@anthropic-ai/claude-code": "2.1.63",
    "@anthropic-ai/claude-agent-sdk": "0.2.63",
    "@musistudio/claude-code-router": "*",
    "pm2": "7.0.3",
    "@agent-infra/mcp-server-browser": "1.2.29",
    "chrome-devtools-mcp": "0.21.0",
    "playwright": "1.56.0"
  }
}
GLOBAL_EOF
echo "=== resolving global npm lockfile ==="
(cd "$GLOBAL_SRC" && npm install --package-lock-only --registry "$NPM_REGISTRY" --no-audit --no-fund 2>&1 | tail -3) \
    || echo "WARN: global lockfile resolution failed; image will fall back to network"
pack_into "$GLOBAL_SRC" "$ROOT/global"

echo "=== Summary ==="
for d in "$ROOT"/*/; do
    if [ -d "$d" ]; then
        n=$(find "$d" -name '*.tgz' | wc -l)
        s=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
        printf '  %-22s %3d files, %s\n' "$(basename "$d")" "$n" "$s"
    fi
done

echo "Done. To use: docker buildx build ... -t aio-sandbox -f docker/Dockerfile.offline"
