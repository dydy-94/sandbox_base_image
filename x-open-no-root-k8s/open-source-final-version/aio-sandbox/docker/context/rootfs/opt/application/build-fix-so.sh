#!/bin/bash
# build-fix-so.sh — re-extract C-extension wheels via the tmp+rename trick.
#
# Called from Dockerfile.offline §15b once at image build time so the
# resulting image has all .so files present and chown'd to `x`.
# Container runtime does NOT need to run this — see the comment block at
# the top of Dockerfile.offline for the rationale.
#
# History:
#  v1: hard-coded list of known-broken packages (pydantic-core, uvloop,
#      httptools, lxml, watchfiles, pyzmq, ...). On Windows
#      docker-desktop the overlay filesystem drops .so files when pip
#      uses atomic os.replace.
#  v2: broad scan — every top-level dir whose pkg directory has no .so
#      *and* has empty __init__.py files gets a flag.  The basic idea
#      was right but the detection was over-eager: it flagged pure-Python
#      packages (anyio, fastapi, jsonschema, lark, mcp, mistune,
#      parso, pathspec, pip, redis, referencing, smmap, traitlets,
#      urllib3, uvicorn, watchdog, typing_inspection, bleach, tzdata,
#      …) which by design have NO .so files.
#  v3 (this file): only trigger when a `.dist-info/METADATA` shows the
#      package needs C extensions AND there are zero-byte `.so` files
#      inside the package. We no longer trust "empty __init__.py" as a
#      signal because pure-Python packages commonly have empty
#      `__init__.py` files.
#
# Fix in v3 also: sweep-so.py was being called with `--wheels-dir ...`
# but originally only accepted positional args — silent FileNotFoundError
# for every package.  sweep-so.py v2 now accepts both forms; this
# script uses `--wheels-dir` to extract every freshly-downloaded wheel
# in one call.
set +e

VE=/opt/server-venv
SITE="$VE/lib/python3.14/site-packages"
PYE="$VE/bin/python"
INDEX="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
WHEELS=/opt/wheels
TMP=/tmp/wheels-fresh

mkdir -p "$TMP"

# Check we have a venv site-packages to scan.
if [ ! -d "$SITE" ]; then
    echo "[build-fix-so] no site-packages at $SITE — skipping"
    exit 0
fi

needs_fix=""
checked=0

# For each top-level package dir that has a *.dist-info sibling, decide
# whether the package *should* contain .so files (per METADATA's
# `Requires-Dist` of `pkg_resources`-style markers — we approximate by
# grepping METADATA for typical C-extension keywords). If yes AND there
# are no .so files in the pkg dir, schedule a rebuild.
for pkgdir in "$SITE"/*/; do
    pkgdir="${pkgdir%/}"
    pkgname="$(basename "$pkgdir")"
    case "$pkgname" in
        # in-house source dirs that aren't pip-installed wheels
        app|vendors|browser_sdk|numpy|pandas|requests|sentry-sdk|cryptography|pip|pip-*|setuptools|pkg_resources) continue ;;
    esac

    # Find the dist-info for this pkg (some pkgs have hyphens replaced
    # with underscores, so glob both forms).
    di=$(ls -d "$SITE"/${pkgname}-*.dist-info 2>/dev/null | head -1)
    if [ -z "$di" ]; then
        di=$(ls -d "$SITE"/${pkgname//_/-}-*.dist-info 2>/dev/null | head -1)
    fi
    [ -z "$di" ] && continue
    [ ! -f "$di/METADATA" ] && continue

    # C-extension hint: the METADATA usually lists 'pip' as the
    # required backend and 'Requires-Python' or filenames like
    # '*.cp313-*.so' / '.abi3.so'. Match those.
    meta="$di/METADATA"
    if grep -qE "\.cp3[0-9]+-|\.abi3\.so|\.cpython-" "$meta" \
       || grep -qE "^Platform: (linux|any|UNKNOWN)" "$meta" \
       || echo "$pkgname" | grep -qE "^(cryptography|numpy|uvloop|httptools|watchfiles|pyzmq|lxml|playwright|pyyaml|greenlet|pyee|pycurl|pyzmq|argon2|cffi)"; then
        :  # candidate
    else
        # No .so expected for this package — skip.
        checked=$((checked + 1))
        continue
    fi

    n_so=$(find "$pkgdir" -name '*.so' 2>/dev/null | wc -l)
    checked=$((checked + 1))
    if [ "$n_so" -eq 0 ]; then
        # count of zero-byte .so would mean a half-written wheel; here
        # we count zero-byte .so FILES inside the dir.
        n_zero=$(find "$pkgdir" -name '*.so' -size 0 2>/dev/null | wc -l)
        if [ "$n_zero" -gt 0 ]; then
            echo "[build-fix-so] $pkgname: zero-byte .so (likely overlay Errno 22) — will rebuild"
            needs_fix="$needs_fix $pkgname"
        fi
    fi
done

echo "[build-fix-so] checked $checked packages (lookng for missing C-extensions)"

if [ -z "$needs_fix" ]; then
    echo "[build-fix-so] nothing to fix — image's venv looks OK"
    exit 0
fi

# Refreshing each affected package via pip + sweep-so.
echo "[build-fix-so] rebuilding:$needs_fix"

# Pre-collect distribution names for each python module name.
declare -A PY_TO_DIST=(
    [yaml]=PyYAML [PIL]=Pillow [cryptography]=cryptography
    [sklearn]=scikit-learn [cv2]=opencv-python
    [gi]=pygobject
)

for pkg in $needs_fix; do
    # Normalize dist name: anyio → anyio; jsonschema_specifications →
    # jsonschema-specifications. But our site dir names are usually
    # already correct as dist names. Translate only well-known ones.
    case "$pkg" in
        bleach) dist=bleach ;;
        fastapi) dist=fastapi ;;
        anyio) dist=anyio ;;
        gitdb) dist=gitdb ;;
        httpcore) dist=httpcore ;;
        ipykernel) dist=ipykernel ;;
        jsonschema) dist=jsonschema ;;
        jsonschema_specifications) dist=jsonschema-specifications ;;
        jupyter_lsp) dist=jupyter-lsp ;;
        jupyter_server) dist=jupyter-server ;;
        lark) dist=lark ;;
        libtmux) dist=libtmux ;;
        mcp) dist=mcp ;;
        mistune) dist=mistune ;;
        nbconvert) dist=nbconvert ;;
        nbformat) dist=nbformat ;;
        notebook_shim) dist=notebook-shim ;;
        parso) dist=parso ;;
        pathspec) dist=pathspec ;;
        pip) dist=pip ;;
        playwright) dist=playwright ;;
        prometheus_client) dist=prometheus-client ;;
        prompt_toolkit) dist=prompt-toolkit ;;
        pydantic) dist=pydantic ;;
        redis) dist=redis ;;
        referencing) dist=referencing ;;
        smmap) dist=smmap ;;
        traitlets) dist=traitlets ;;
        typing_inspection) dist=typing-inspection ;;
        tzdata) dist=tzdata ;;
        urllib3) dist=urllib3 ;;
        uvicorn) dist=uvicorn ;;
        watchdog) dist=watchdog ;;
        *) dist="$pkg" ;;
    esac

    echo "[build-fix-so] refreshing $pkg ($dist)"
    rm -f "$TMP"/* 2>/dev/null
    $PYE -m pip download --quiet --no-deps --dest "$TMP" \
        --index-url "$INDEX" "$dist" 2>&1 | tail -3
    if ls "$TMP"/*.whl >/dev/null 2>&1; then
        $PYE /opt/application/sweep-so.py --wheels-dir "$TMP" --site "$SITE" 2>&1 | tail -3
    else
        echo "[build-fix-so]   no wheel fetched for $dist — skipped"
    fi
done

# Best-effort ownership fix.
id x >/dev/null 2>&1 && chown -R 1000:1000 "$SITE" 2>/dev/null || true
echo "[build-fix-so] done"
