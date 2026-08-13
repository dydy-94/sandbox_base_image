#!/bin/bash
# init-once.sh: runtime-only fixups for aio-sandbox (no-root supervisor).
#
# Boot sequence:
#   1. Idempotent init marker (rerun with FORCE_INIT=1).
#   2. Host-derived nginx envsubst (PUBLIC_PORT, PUBLIC_LISTEN_IPV4/V6, RUNTIME_LISTEN).
#   3. Defensive chown: Dockerfile bakes ownership at build time, but
#      volume-mounted paths may be re-mounted fresh as root at runtime.
#   4. `exec` supervisord so it becomes PID 1 and receives docker signals
#      directly. Supervisord drops to user=x via its own `user=x` setting
#      in /opt/gem/supervisord.conf — no gosu/su-exec wrapper needed.
#
# Why PID 1 = supervisord (not init-once.sh):
#   * Kernel forwards SIGTERM/SIGINT from `docker stop` to PID 1 only.
#   * PID 1 must reap orphaned children; bash does this poorly.
#   * supervisord manages child lifecycle natively.
set -euo pipefail

MARKER="/var/lib/aio-sandbox/init.done"
RUNTIME_UID="${USER_UID:-1000}"
RUNTIME_GID="${USER_GID:-1000}"
RUNTIME_USER="${USER:-x}"
SUPERVISORD_CONF="${SUPERVISORD_CONF:-/opt/gem/supervisord.conf}"

log() { echo "[init-once] $(date -u +'%Y-%m-%dT%H:%M:%SZ') $*"; }

# ---------- 1. idempotent init ----------
if [ -f "$MARKER" ] && [ "${FORCE_INIT:-0}" = "0" ]; then
  log "marker ${MARKER} exists, skipping init (FORCE_INIT=1 to rerun)"
else
  mkdir -p /var/lib/aio-sandbox

  # ---------- 2. nginx envsubst (host-derived vars) ----------
  if [ -f /opt/gem/nginx-server-without-auth.conf ]; then
    cp -f /opt/gem/nginx-server-without-auth.conf /opt/gem/nginx-server-active.conf
    export PUBLIC_PORT="${PUBLIC_PORT:-8080}"
    export PUBLIC_LISTEN_IPV4="${PUBLIC_LISTEN_IPV4:-0.0.0.0}"
    export PUBLIC_LISTEN_IPV6="${PUBLIC_LISTEN_IPV6:-[::]}"
    export RUNTIME_LISTEN=""
    envsubst '${PUBLIC_LISTEN_IPV4} ${PUBLIC_LISTEN_IPV6} ${PUBLIC_PORT} ${RUNTIME_LISTEN}' \
      < /opt/gem/nginx-server-active.conf > /tmp/active.conf
    cp /tmp/active.conf /opt/gem/nginx-server-active.conf
    log "nginx active config rendered for ${PUBLIC_LISTEN_IPV4}:${PUBLIC_PORT}"
  fi

  # ---------- 3. ensure runtime paths are writable by ${RUNTIME_USER} ----------
  # The Dockerfile bakes ownership at build time (every file under /home/x,
  # /var/lib/aio-sandbox, ... is already 1000:1000 when the image is built).
  # A full `chown -R` on every boot is wasteful — /home/x alone has tens of
  # thousands of files (global npm packages, daytona, ...) and can stall the
  # boot for minutes on slow filesystems. So we only re-chown when the
  # TOP-LEVEL owner actually differs from ${RUNTIME_UID}, which is the case
  # when a volume is mounted fresh as root (docker -v /host:/home/x). In the
  # default (no volume) case this loop is skipped entirely and startup stays
  # fast.
  for d in \
      /var/lib/aio-sandbox \
      /var/log/aio-sandbox \
      /var/log/gem \
      /tmp/runtime-x \
      /tmp/dbus-session-bus \
      /home/${RUNTIME_USER} \
      /home/${RUNTIME_USER}/.run; do
    [ -e "$d" ] || continue
    if [ "$(id -u)" = "0" ]; then
      cur_uid="$(stat -c %u "$d" 2>/dev/null || echo -1)"
      if [ "${cur_uid}" != "${RUNTIME_UID}" ]; then
        echo "[init-once] chowning ${d} (owner ${cur_uid} -> ${RUNTIME_UID})"
        chown -R "${RUNTIME_UID}:${RUNTIME_GID}" "$d" 2>/dev/null || true
      fi
    fi
  done

  # ---------- 4. mark init complete ----------
  date -u +"%Y-%m-%dT%H:%M:%SZ" > "$MARKER"
  [ "$(id -u)" = "0" ] && chown "${RUNTIME_UID}:${RUNTIME_GID}" "$MARKER" 2>/dev/null || true
  log "init marker written"
fi

# ---------- 5. handoff to run.sh (which execs supervisord as PID 1) ----------
# run.sh exports the %(ENV_*)s interpolation vars used by supervisord child
# configs (AUTOSTART_*, VNC ports, browser ports, pm2 pre-warm,
# RUN_HOOK_*) and then `exec supervisord -n -c ${SUPERVISORD_CONF}`. supervisord
# itself drops to ${RUNTIME_USER} via `user=x` in ${SUPERVISORD_CONF}.
log "handing off to /opt/application/run.sh (supervisord will drop to ${RUNTIME_USER} via user=x)"
exec /opt/application/run.sh "$@"