#!/bin/bash
# init-once.sh: runtime-only fixups for aio-sandbox.
#
# After v10 everything deterministic was moved into the image at build
# time (Dockerfile.offline §15d, §15e). Only operations that require
# host-derived env vars (PUBLIC_PORT_LISTEN_IPV4) stay here.
#
# Idempotent: safe to run on every boot. Set FORCE_INIT=1 to rerun.
set -e

MARKER="/var/lib/aio-sandbox/init.done"

if [ -f "$MARKER" ] && [ "${FORCE_INIT:-0}" = "0" ]; then
    echo "[init-once] marker $MARKER exists, skipping (FORCE_INIT=1 to rerun)"
else
    mkdir -p /var/lib/aio-sandbox

    # ---- 2. Substitute host-derived nginx template vars
    #         (PUBLIC_PORT, PUBLIC_LISTEN_IPV4/V6). The Dockerfile has
    #         defaults baked; this re-runs envsubst with the actual
    #         host environment so the public binding reflects the
    #         chosen port mapping (e.g. when USER runs with
    #         `-p 18080:8080`).
    if [ -f /opt/gem/nginx-server-without-auth.conf ]; then
        cp -f /opt/gem/nginx-server-without-auth.conf /opt/gem/nginx-server-active.conf
        export PUBLIC_PORT="${PUBLIC_PORT:-8080}"
        export PUBLIC_LISTEN_IPV4="${PUBLIC_LISTEN_IPV4:-0.0.0.0}"
        export PUBLIC_LISTEN_IPV6="${PUBLIC_LISTEN_IPV6:-[::]}"
        export RUNTIME_LISTEN=""
        envsubst '${PUBLIC_LISTEN_IPV4} ${PUBLIC_LISTEN_IPV6} ${PUBLIC_PORT} ${RUNTIME_LISTEN}' \
            < /opt/gem/nginx-server-active.conf > /tmp/active.conf
        cp /tmp/active.conf /opt/gem/nginx-server-active.conf
    fi

    # ---- 3. Mark init complete (idempotency marker).
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$MARKER"
fi

# ---- 4. Hand off to the original entrypoint.
exec /opt/application/run.sh "$@"
