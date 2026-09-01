#!/bin/bash
#
# AIO Sandbox + GemBrowser Merged Entrypoint
# This script initializes the sandbox environment and starts supervisord.
#

set -e

# ----------------------
# Timing Functions
# ----------------------
TIMING_START=$(date +%s%3N)
TIMING_LAST=$TIMING_START

timing_checkpoint() {
  local checkpoint_name="$1"
  local now=$(date +%s%3N)
  local elapsed_from_start=$((now - TIMING_START))
  local elapsed_from_last=$((now - TIMING_LAST))
  echo "$(date '+%Y-%m-%d %H:%M:%S,%3N') TIMING [${checkpoint_name}] +${elapsed_from_last}ms (total: ${elapsed_from_start}ms)"
  TIMING_LAST=$now
}

# ----------------------
# Logging Function
# ----------------------
log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S,%3N') INFO $@"
}

normalize_bool() {
  local value
  value="$(echo -n "${1:-}" | xargs | tr '[:upper:]' '[:lower:]')"
  case "$value" in
    1|true|yes|on) echo "true" ;;
    *) echo "false" ;;
  esac
}

normalize_node_version() {
  case "${1:-}" in
    20|node20) echo "node20" ;;
    22|node22|'') echo "node22" ;;
    24|node24) echo "node24" ;;
    *)
      log "WARNING: Unknown NODE_VERSION/NODE_CODE_EXEC_VERSION '${1}', falling back to node22"
      echo "node22"
      ;;
  esac
}

resolve_node_repl_port() {
  local env_name="$1"
  local default_port="$2"
  local current_value="${!env_name:-}"

  if [ -z "${current_value}" ]; then
    printf '%s' "${default_port}"
    return 0
  fi

  if ! [[ "${current_value}" =~ ^[0-9]+$ ]] || [ "${current_value}" -lt 1 ] || [ "${current_value}" -gt 65535 ]; then
    log "ERROR: ${env_name} must be a valid TCP port, got '${current_value}'"
    exit 1
  fi

  printf '%s' "${current_value}"
}

setup_xdg_runtime_dir() {
  if [ -z "${XDG_RUNTIME_DIR:-}" ]; then
    export XDG_RUNTIME_DIR="/run/user/${USER_UID}"
    log "XDG_RUNTIME_DIR not set, defaulting to ${XDG_RUNTIME_DIR}"
  else
    case "$XDG_RUNTIME_DIR" in
      /*) ;;
      *)
        log "ERROR: XDG_RUNTIME_DIR must be an absolute path, got: ${XDG_RUNTIME_DIR}"
        exit 1
        ;;
    esac
    log "Using XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR}"
  fi

  # k8s runAsUser 1000 (non-root): /run/user/1000 is pre-created at build
  # time (Dockerfile 14m2) since a uid-1000 process cannot mkdir under /run.
  if [ "$(id -u)" = "0" ]; then
    mkdir -p /run/user
    mkdir -p "$XDG_RUNTIME_DIR"
    chown $USER:$USER "$XDG_RUNTIME_DIR"
    chmod 700 "$XDG_RUNTIME_DIR"
  else
    mkdir -p "$XDG_RUNTIME_DIR" 2>/dev/null || true
    log "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR} (pre-created at build time)"
  fi
}

setup_dbus_session_socket() {
  export AIO_DBUS_SESSION_SOCKET="${XDG_RUNTIME_DIR}/dbus-session-bus"

  if [ "$AUTOSTART_CJK_IME" = "true" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${AIO_DBUS_SESSION_SOCKET}"
    log "CJK IME enabled (fcitx5)"
  fi
}

# ----------------------
# Lifecycle Hook Runner
# ----------------------
# Usage: run_hook <hook_name> <env_commands>
run_hook() {
  local hook_name="$1"
  local hook_commands="$2"

  # Trim leading and trailing whitespace while preserving internal formatting
  local trimmed_commands
  trimmed_commands="$(printf '%s' "$hook_commands" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  [ -z "$trimmed_commands" ] && return 0

  local strict_mode
  strict_mode="$(echo -n "${RUN_HOOKS_STRICT:-false}" | xargs | tr '[:upper:]' '[:lower:]')"

  log "Executing $hook_name hook..."
  if [ "$strict_mode" = "true" ]; then
    bash -c "$trimmed_commands"
  else
    bash -c "$trimmed_commands" || log "WARNING: $hook_name hook failed with exit code $? (RUN_HOOKS_STRICT=false, continuing...)"
  fi
  log "$hook_name hook completed."
}

log "Starting AIO Sandbox entrypoint..."
timing_checkpoint "entrypoint_start"

# ----------------------
# Convert DISABLE_* to supervisord autostart format
# ----------------------
# Default DISABLE_* env vars to "true" (disabled) so dashboards and nginx
# locations can be safely envsubst'd even if the operator didn't set them.
# If you want a feature on, set DISABLE_<feature>=false explicitly.
export DISABLE_JUPYTER="${DISABLE_JUPYTER:-true}"
export DISABLE_CODE_SERVER="${DISABLE_CODE_SERVER:-true}"
# VNC + chrome are core sandbox features: enabled by default.
# DISABLE_BROWSER_UI only controls the /browser-ui panel in the dashboard —
# the chrome process itself (CDP 9222) stays up either way.
export DISABLE_BROWSER="${DISABLE_BROWSER:-false}"
export DISABLE_MCP_BROWSER="${DISABLE_MCP_BROWSER:-true}"
export DISABLE_VNC="${DISABLE_VNC:-false}"
export DISABLE_OPENCODE="${DISABLE_OPENCODE:-true}"
export DISABLE_BROWSER_UI="${DISABLE_BROWSER_UI:-true}"
export AUTOSTART_JUPYTER=$([ "$DISABLE_JUPYTER" != "true" ] && echo "true" || echo "false")
export AUTOSTART_CODE_SERVER=$([ "$DISABLE_CODE_SERVER" != "true" ] && echo "true" || echo "false")
export AUTOSTART_BROWSER=$([ "$DISABLE_BROWSER" != "true" ] && echo "true" || echo "false")
# MCP_BROWSER depends on browser, disable it if browser is disabled
export AUTOSTART_MCP_BROWSER=$([ "$DISABLE_MCP_BROWSER" != "true" ] && [ "$DISABLE_BROWSER" != "true" ] && echo "true" || echo "false")

# VNC/GUI services (tigervnc, websocat, openbox)
export AUTOSTART_VNC=$([ "$DISABLE_VNC" != "true" ] && echo "true" || echo "false")

# CJK Input Method (fcitx5 + dbus), requires VNC and browser to be enabled
export AUTOSTART_CJK_IME=$([ "$ENABLE_CJK_IME" = "true" ] && [ "$DISABLE_VNC" != "true" ] && [ "$DISABLE_BROWSER" != "true" ] && echo "true" || echo "false")
if [ "$AUTOSTART_CJK_IME" = "true" ]; then
    export GTK_IM_MODULE=fcitx
    export QT_IM_MODULE=fcitx
    export XMODIFIERS="@im=fcitx"
fi

# Node.js REPL config
# - `DISABLE_NODEJS_REPL=true` disables both autostart and on-demand startup
# - `NODEJS_REPL_PORT` overrides the selected default version port
# - `NODEJS_REPL_PORT_20/22/24` override specific version ports
export NODE_VERSION="$(normalize_node_version "${NODE_VERSION:-${NODE_CODE_EXEC_VERSION:-node22}}")"
export DISABLE_NODEJS_REPL="$(normalize_bool "${DISABLE_NODEJS_REPL:-false}")"

export NODEJS_REPL_PORT_20="$(resolve_node_repl_port NODEJS_REPL_PORT_20 8192)"
export NODEJS_REPL_PORT_22="$(resolve_node_repl_port NODEJS_REPL_PORT_22 8092)"
export NODEJS_REPL_PORT_24="$(resolve_node_repl_port NODEJS_REPL_PORT_24 8392)"

if [ -n "${NODEJS_REPL_PORT:-}" ]; then
  LEGACY_NODEJS_REPL_PORT="$(resolve_node_repl_port NODEJS_REPL_PORT 0)"
  case "$NODE_VERSION" in
    node20) export NODEJS_REPL_PORT_20="${LEGACY_NODEJS_REPL_PORT}" ;;
    node22) export NODEJS_REPL_PORT_22="${LEGACY_NODEJS_REPL_PORT}" ;;
    node24) export NODEJS_REPL_PORT_24="${LEGACY_NODEJS_REPL_PORT}" ;;
  esac
fi

if [ "$DISABLE_NODEJS_REPL" = "true" ]; then
  export AUTOSTART_NODEJS_REPL_20="false"
  export AUTOSTART_NODEJS_REPL_22="false"
  export AUTOSTART_NODEJS_REPL_24="false"
else
  export AUTOSTART_NODEJS_REPL_20=$([ "$NODE_VERSION" = "node20" ] && echo "true" || echo "false")
  export AUTOSTART_NODEJS_REPL_22=$([ "$NODE_VERSION" = "node22" ] && echo "true" || echo "false")
  export AUTOSTART_NODEJS_REPL_24=$([ "$NODE_VERSION" = "node24" ] && echo "true" || echo "false")
fi

log "Node.js REPL config: disabled=${DISABLE_NODEJS_REPL}, default_version=${NODE_VERSION}, ports={node20:${NODEJS_REPL_PORT_20},node22:${NODEJS_REPL_PORT_22},node24:${NODEJS_REPL_PORT_24}}"

# ----------------------
# Logging Configuration
# ----------------------
export LOG_TOOL_TRACE="${LOG_TOOL_TRACE:-false}"
export LOG_STDOUT_SERVER="${LOG_STDOUT_SERVER:-true}"

log "Python tool trace: ${LOG_TOOL_TRACE}"
log "Log stdout server: ${LOG_STDOUT_SERVER}"

# ----------------------
# Run Init Hook (very early, before user creation)
# ----------------------
run_hook "RUN_HOOK_INIT" "$RUN_HOOK_INIT"
timing_checkpoint "init_hook"

# ----------------------
# Create Non-root User
# ----------------------
log "Creating user ('$USER') with UID ($USER_UID) and GID ($USER_GID)..."
# Pre-create the group with --force-style tolerant semantics: ignore
# "already exists" errors but propagate real failures.
if ! getent group $USER >/dev/null; then
    set +e
    groupadd --force --gid "$USER_GID" "$USER" 2>&1
    rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        # Re-check: maybe a different group already holds this GID; in
        # that case, make the user share that gid.
        if ! getent group $USER >/dev/null; then
            existing_group="$(getent group "$USER_GID" | cut -d: -f1)"
            if [ -n "$existing_group" ]; then
                log "GID '$USER_GID' is taken by group '$existing_group'; sharing"
                USER_GID_NAME="$existing_group"
            else
                log "ERROR: cannot create group $USER with GID $USER_GID (rc=$rc)"
                exit 1
            fi
        fi
    fi
fi
if ! id -u $USER >/dev/null 2>&1; then
    set +e
    if [ -n "${USER_GID_NAME:-}" ]; then
        useradd -o --uid "$USER_UID" --gid "$USER_GID_NAME" --shell /bin/bash --create-home -m "$USER" 2>&1
    else
        useradd -o --uid "$USER_UID" --gid "$USER_GID" --shell /bin/bash --create-home -m "$USER" 2>&1
    fi
    rc=$?
    set -e
    if [ $rc -ne 0 ]; then
        # Fall back: see if some user already has this UID; if so we
        # simply reuse the username as a label (so chown 1000:1000 works
        # because it resolves to "ubuntu:ubuntu" by uid:gid). If that
        # doesn't exist either, downgrade to root.
        if id -u $USER >/dev/null 2>&1; then
            log "user '$USER' ended up with shared UID ${USER_UID}; continuing"
        else
            log "WARN: useradd failed (rc=$rc) for $USER; will use uid ${USER_UID} directly"
            USER_REAL_NAME="$(getent passwd ${USER_UID} | cut -d: -f1)"
            if [ -n "$USER_REAL_NAME" ]; then
                log "  -> resolved existing uid ${USER_UID} to user '${USER_REAL_NAME}'"
                USER="$USER_REAL_NAME"
            fi
        fi
    fi
fi

export WORKSPACE="${WORKSPACE:-/home/$USER}"
export BROWSER_DOWNLOAD_DIR_EFFECTIVE="${BROWSER_DOWNLOAD_DIR:-${WORKSPACE}/Downloads}"

# x has NO sudo: not in the sudo group, no sudoers entries. Code running as
# x (browser pages, pasted commands) can never escalate to root, whether the
# container boots as root (docker) or uid-1000 (k8s runAsUser:1000).
# Ensure home directory is owned by the user (volume mounts may be root-owned).
# Tolerate ':$USER' group syntax when only the user (uid) exists: chown
# falls back to user-only if the group doesn't exist.
CHOWN_GROUP="$USER"
if ! getent group "$CHOWN_GROUP" >/dev/null; then
    CHOWN_GROUP="$(getent group "$USER_GID" | cut -d: -f1)"
    [ -z "$CHOWN_GROUP" ] && CHOWN_GROUP=""
fi
set +e
if [ -n "$CHOWN_GROUP" ]; then
    chown "$USER:$CHOWN_GROUP" "/home/$USER" 2>&1
else
    chown "$USER" "/home/$USER" 2>&1
fi
set -e
timing_checkpoint "create_user"

# ----------------------
# X11 Setup (from gembrowser)
# ----------------------
log "Setting up X11 permissions..."
rm -rf /tmp/.X11-unix  # Clean stale sockets on restart
# Clean stale X lock files: a hard kill (docker restart/k8s kill) leaves
# /tmp/.X99-lock behind, which makes tigervnc abort with "Server is already
# active for display 99" and cascades to chrome (no $DISPLAY) + nginx-wait.
rm -f /tmp/.X99-lock /tmp/.X1-lock /tmp/.X0-lock 2>/dev/null || true
mkdir -p /tmp/.X11-unix
chmod 1777 /tmp/.X11-unix
set +e
chown "$USER" /tmp/.X11-unix/ 2>&1
set -e

# ----------------------
# Create Necessary Directories
# ----------------------
log "Creating necessary directories..."
export LOG_DIR="${LOG_DIR:-/var/log/aio-sandbox}"
mkdir -p "$LOG_DIR"
chmod 1777 "$LOG_DIR"
chown $USER "$LOG_DIR" 2>/dev/null || true
setup_xdg_runtime_dir
setup_dbus_session_socket

log "Creating service state directory..."
mkdir -p /var/lib/aio-sandbox
chown $USER:$USER /var/lib/aio-sandbox

log "Ensuring workspace download directory exists..."
mkdir -p "$WORKSPACE" "$BROWSER_DOWNLOAD_DIR_EFFECTIVE"
chown $USER:$USER "$BROWSER_DOWNLOAD_DIR_EFFECTIVE"
log "Browser downloads directory: ${BROWSER_DOWNLOAD_DIR_EFFECTIVE}"

# nginx temp/cache dirs are pre-created at build time (Dockerfile 14m2)
# with the exact nobody ownership; only root needs to (re)create them here
# (e.g. docker run as root). uid-1000 launches must skip mkdir/chown under
# /var/lib to avoid "Permission denied".
if [ "$(id -u)" = "0" ]; then
  log "Setting up Nginx directories..."
  mkdir -p /var/lib/nginx
  chmod 1777 /var/lib/nginx
  chown nobody /var/lib/nginx

  NGINX_RUNTIME_DIR=/var/lib/aio-sandbox/nginx
  mkdir -p "${NGINX_RUNTIME_DIR}"
  chmod 755 /var/lib/aio-sandbox "${NGINX_RUNTIME_DIR}"
  chown nobody:root "${NGINX_RUNTIME_DIR}"
  for temp_dir in body proxy fastcgi scgi uwsgi; do
    mkdir -p "${NGINX_RUNTIME_DIR}/${temp_dir}"
    chown nobody:root "${NGINX_RUNTIME_DIR}/${temp_dir}"
    chmod 700 "${NGINX_RUNTIME_DIR}/${temp_dir}"
  done
else
  log "Nginx directories already pre-created at build time (non-root launch)"
fi

if [ -d /opt/jupyter ]; then
    chown -R $USER:$USER /opt/jupyter 2>/dev/null || true
fi
timing_checkpoint "create_directories"

# Control whether bundled skills are copied into user home.
COPY_SKILLS_TO_USER_HOME_RAW="$(echo -n "${AIO_CLI_SKILL_ENABLED:-false}" | xargs | tr '[:upper:]' '[:lower:]')"
if [ "${COPY_SKILLS_TO_USER_HOME_RAW}" = "true" ]; then
  COPY_SKILLS_TO_USER_HOME="true"
else
  COPY_SKILLS_TO_USER_HOME="false"
fi
log "Copy bundled skills to user home: ${COPY_SKILLS_TO_USER_HOME}"

# ----------------------
# Parallel Setup: User config + DNS/Nginx config run concurrently
# ----------------------
log "Starting parallel setup (user_setup + config)..."

# Group A: User-specific Setup (runs as $USER)
(
  # Pre-render opencode config: OPENCODE_JSON > OPENCODE_* env vars > template
  OPENCODE_RENDERED=""
  OPENCODE_TPL="/opt/gem/opencode/config.json"
  if [ -n "${OPENCODE_JSON}" ]; then
    OPENCODE_RENDERED="$(mktemp)"
    printf "%s\n" "${OPENCODE_JSON}" > "${OPENCODE_RENDERED}"
  elif [ -n "${OPENCODE_API_KEY}" ] && [ -n "${OPENCODE_MODEL}" ] && [ -f "${OPENCODE_TPL}" ]; then
    OPENCODE_PROVIDER="${OPENCODE_PROVIDER:-ark}"
    OPENCODE_BASE_URL="${OPENCODE_BASE_URL:-https://ark.cn-beijing.volces.com/api/v3}"
    OPENCODE_RENDERED="$(mktemp)"
    # Auto-detect SDK: anthropic protocol if URL contains "anthropic", otherwise openai-compatible
    if [ -z "${OPENCODE_PROVIDER_NPM}" ]; then
      case "${OPENCODE_BASE_URL}" in
        *anthropic*) OPENCODE_PROVIDER_NPM="@ai-sdk/anthropic" ;;
        *)           OPENCODE_PROVIDER_NPM="@ai-sdk/openai-compatible" ;;
      esac
    fi
    jq --arg provider "${OPENCODE_PROVIDER}" \
       --arg model "${OPENCODE_PROVIDER}/${OPENCODE_MODEL}" \
       --arg url "${OPENCODE_BASE_URL}" \
       --arg key "${OPENCODE_API_KEY}" \
       --arg mid "${OPENCODE_MODEL}" \
       --arg npm "${OPENCODE_PROVIDER_NPM}" \
       '.model = $model
        | .provider[$provider].npm = $npm
        | .provider[$provider].options.baseURL = $url
        | .provider[$provider].options.apiKey = $key
        | .provider[$provider].models[$mid] = {}' \
       "${OPENCODE_TPL}" > "${OPENCODE_RENDERED}"
  fi
  [ -n "${OPENCODE_RENDERED}" ] && chmod 644 "${OPENCODE_RENDERED}" || true

  USER_SETUP_SCRIPT=$(cat <<'EOF'
mkdir -p /home/$USER/.npm-global/lib/node_modules

# 1. Copy default configurations
cp -f /opt/gem/bashrc /home/$USER/.bashrc

# code-server
mkdir -p /home/$USER/.config/code-server /home/$USER/.local/share/code-server \
     && chmod -R 755 /home/$USER/.local/share/code-server/
cp -rf /opt/gem/vscode /home/$USER/.config/code-server/vscode

# jupyter
cp -rf /opt/gem/jupyter /home/$USER/.jupyter 2>/dev/null || true

# matplotlib
mkdir -p /home/$USER/.config/matplotlib
cp -f /opt/gem/matplotlibrc /home/$USER/.config/matplotlib/matplotlibrc
rm -rf /home/$USER/.cache/matplotlib/fontlist-*.json 2>/dev/null || true

# fcitx5 (CJK input method, only when enabled)
if [ -d "/opt/gem/fcitx5" ] && [ ! -f "/home/$USER/.config/fcitx5/profile" ]; then
  mkdir -p /home/$USER/.config/fcitx5/conf
  cp -r /opt/gem/fcitx5/* /home/$USER/.config/fcitx5/
fi

# 2. Merge staged configs
if [ -d "/opt/aio-staged-configs" ]; then
  rsync -a /opt/aio-staged-configs/ /home/$USER/
fi

# 3. Agent skills
if [ "${AIO_CLI_SKILL_ENABLED}" = "true" ] && [ -d "/opt/skills" ]; then
  mkdir -p /home/$USER/.agents
  cp -rf /opt/skills /home/$USER/.agents/skills
fi

# 4. Browser preferences
touch "$HOME/.Xauthority"
mkdir -p "$HOME/.config/browser/Default"
cp "/opt/gem/preferences.json" "$HOME/.config/browser/Default/Preferences"

# Configure browser language in Chrome Preferences (--lang flag alone is not enough;
# Chrome reads intl.selected_languages from the profile and ignores --lang if present)
BROWSER_LANGS="${BROWSER_LANG}"
BROWSER_BASE="${BROWSER_LANG%%-*}"
# Add base language if BROWSER_LANG has a region subtag (e.g. zh-CN -> zh)
case "${BROWSER_LANG}" in *-*) BROWSER_LANGS="${BROWSER_LANGS},${BROWSER_BASE}";; esac
# Append en-US,en fallback (skip items already present)
[ "${BROWSER_LANG}" != "en-US" ] && [ "${BROWSER_BASE}" != "en" ] && BROWSER_LANGS="${BROWSER_LANGS},en-US,en"
[ "${BROWSER_LANG}" != "en-US" ] && [ "${BROWSER_BASE}" = "en" ] && BROWSER_LANGS="${BROWSER_LANGS},en-US"
jq --arg langs "${BROWSER_LANGS}" \
  '.intl.selected_languages = $langs | .intl.accept_languages = $langs' \
  "$HOME/.config/browser/Default/Preferences" > "$HOME/.config/browser/Default/Preferences.tmp" \
  && mv "$HOME/.config/browser/Default/Preferences.tmp" "$HOME/.config/browser/Default/Preferences"

# Configure download directory (explicit override or default WORKSPACE/Downloads)
mkdir -p "${BROWSER_DOWNLOAD_DIR_EFFECTIVE}"
jq --arg dir "${BROWSER_DOWNLOAD_DIR_EFFECTIVE}" \
  '.download.default_directory = $dir | .download.directory_upgrade = true | .savefile.default_directory = $dir' \
  "$HOME/.config/browser/Default/Preferences" > "$HOME/.config/browser/Default/Preferences.tmp" \
  && mv "$HOME/.config/browser/Default/Preferences.tmp" "$HOME/.config/browser/Default/Preferences"

# Mark the profile as already-initialized so Chrome skips the first-run wizard
# and the sign-in prompt. Mirrors BROWSER_NO_FIRST_RUN (default true) which adds
# the matching command-line flags.
if [ "${BROWSER_NO_FIRST_RUN:-true}" != "false" ]; then
  jq '
    .browser.first_run_beacon = true |
    .profile.gaia_info = {} |
    .profile.exit_type = "Normal" |
    .signin.allowed = false |
    .sync_promo.show_on_first_run_allowed = false
  ' "$HOME/.config/browser/Default/Preferences" > "$HOME/.config/browser/Default/Preferences.tmp" \
    && mv "$HOME/.config/browser/Default/Preferences.tmp" "$HOME/.config/browser/Default/Preferences"
fi

# opencode config (rendered by root before su, merge with user's existing config)
if [ -n "${OPENCODE_RENDERED}" ] && [ -f "${OPENCODE_RENDERED}" ]; then
  mkdir -p /home/$USER/.config/opencode
  OPENCODE_TARGET="/home/$USER/.config/opencode/config.json"
  cp -f "${OPENCODE_RENDERED}" "${OPENCODE_TARGET}"
fi
rm -f "${OPENCODE_RENDERED}" 2>/dev/null || true
EOF
)
  # k8s runAsUser 1000 (non-root): `su -` needs root, so run the same setup
  # script directly as the current user (we already ARE x). Root launches
  # keep the original su so env/HOME match a fresh login.
  USER_SETUP_ENVS="AIO_CLI_SKILL_ENABLED=${COPY_SKILLS_TO_USER_HOME} BROWSER_LANG=${BROWSER_LANG:-en-US} BROWSER_DOWNLOAD_DIR_EFFECTIVE='${BROWSER_DOWNLOAD_DIR_EFFECTIVE}' BROWSER_NO_FIRST_RUN=${BROWSER_NO_FIRST_RUN:-true} OPENCODE_RENDERED='${OPENCODE_RENDERED}'"
  if [ "$(id -u)" = "0" ]; then
    su - $USER -c "${USER_SETUP_ENVS} bash -s" <<< "${USER_SETUP_SCRIPT}"
  else
    log "non-root launch: running user config setup directly (no su)"
    HOME="/home/$USER" USER="$USER" eval "${USER_SETUP_ENVS} bash -s" <<< "${USER_SETUP_SCRIPT}"
  fi
) &
PID_USER_SETUP=$!

# bwrap (bubblewrap) runs rootless via unprivileged user namespaces. x has
# no sudo and /proc/sys is read-only in non-privileged containers, so the
# kernel flag must be granted by the platform (k8s securityContext.sysctls /
# privileged / seccomp=unconfined). Nothing to do here at boot.

# Group B: DNS + Nginx Configuration (independent, runs as root)
(
  # Chrome enterprise policies live in /etc/opt/chrome/policies/managed
  # (google-chrome-stable deb; NOT /etc/browser/policies/managed which is the
  # chromium path). Pre-created at build time so uid-1000 x can write it.
  CHROME_POLICY_DIR=/etc/opt/chrome/policies/managed

  # DNS over HTTPS Configuration
  TRIMMED_DOH_TEMPLATES="$(echo -n "$DNS_OVER_HTTPS_TEMPLATES" | xargs)"
  if [ -n "$TRIMMED_DOH_TEMPLATES" ]; then
    if [ -w "$CHROME_POLICY_DIR" ] 2>/dev/null || [ "$(id -u)" = "0" ]; then
      mkdir -p "$CHROME_POLICY_DIR"
      cat >"$CHROME_POLICY_DIR/dns_over_https.json" <<DOHEOF
{
  "DnsOverHttpsMode": "secure",
  "DnsOverHttpsTemplates": "$TRIMMED_DOH_TEMPLATES"
}
DOHEOF
    else
      log "WARNING: skip DoH policy (no write access to $CHROME_POLICY_DIR as uid $(id -u))"
    fi
  fi

  # URL Blocklist/Allowlist Policy
  TRIMMED_BLOCKLIST="$(echo -n "${BROWSER_URL_BLOCKLIST:-}" | xargs)"
  TRIMMED_ALLOWLIST="$(echo -n "${BROWSER_URL_ALLOWLIST:-}" | xargs)"
  if [ -n "$TRIMMED_BLOCKLIST" ] || [ -n "$TRIMMED_ALLOWLIST" ]; then
    if [ -w "$CHROME_POLICY_DIR" ] 2>/dev/null || [ "$(id -u)" = "0" ]; then
      URL_POLICY="{}"
      if [ -n "$TRIMMED_BLOCKLIST" ]; then
        URL_POLICY=$(echo "$URL_POLICY" | jq --argjson list "$(echo "$TRIMMED_BLOCKLIST" | jq -R 'split(",") | map(gsub("^\\s+|\\s+$";""))')" '.URLBlocklist = $list')
      fi
      if [ -n "$TRIMMED_ALLOWLIST" ]; then
        URL_POLICY=$(echo "$URL_POLICY" | jq --argjson list "$(echo "$TRIMMED_ALLOWLIST" | jq -R 'split(",") | map(gsub("^\\s+|\\s+$";""))')" '.URLAllowlist = $list')
      fi
      echo "$URL_POLICY" > "$CHROME_POLICY_DIR/url_filter.json"
    else
      log "WARNING: skip URL policy (no write access to $CHROME_POLICY_DIR as uid $(id -u))"
    fi
  fi

  # Nginx listen address configuration
  if [ "${FAAS_SANDBOX_RUNTIME_INJECTION_ENABLE_SANDBOXD}" = "true" ]; then
    export PUBLIC_LISTEN_IPV4="127.0.0.1"
    export PUBLIC_LISTEN_IPV6="[::1]"
  else
    export PUBLIC_LISTEN_IPV4="${PUBLIC_LISTEN_IPV4:-0.0.0.0}"
    export PUBLIC_LISTEN_IPV6="${PUBLIC_LISTEN_IPV6:-[::]}"
  fi

  # Runtime port: platform health check port (e.g. ByteFaaS _BYTEFAAS_RUNTIME_PORT)
  # In sandboxd mode, sandboxd occupies the runtime port and proxies to nginx on PUBLIC_PORT,
  # so nginx must also listen on the runtime port for direct platform health checks.
  if [ "${FAAS_SANDBOX_RUNTIME_INJECTION_ENABLE_SANDBOXD}" = "true" ] && [ -n "$_BYTEFAAS_RUNTIME_PORT" ] && [ "$_BYTEFAAS_RUNTIME_PORT" != "$PUBLIC_PORT" ]; then
    export RUNTIME_LISTEN="listen ${PUBLIC_LISTEN_IPV4}:${_BYTEFAAS_RUNTIME_PORT}; listen ${PUBLIC_LISTEN_IPV6}:${_BYTEFAAS_RUNTIME_PORT};"
  else
    export RUNTIME_LISTEN=""
  fi

  # Gateway timeout policy:
  # - connect timeout stays short for fast failure when upstream is unreachable
  # - read/send timeouts are treated as long idle timeouts, not business timeouts
  export NGINX_PROXY_CONNECT_TIMEOUT="${NGINX_PROXY_CONNECT_TIMEOUT:-5s}"
  export NGINX_API_IDLE_TIMEOUT="${NGINX_API_IDLE_TIMEOUT:-86400s}"
  export NGINX_SESSION_IDLE_TIMEOUT="${NGINX_SESSION_IDLE_TIMEOUT:-86400s}"
  # Rendered nginx configs are generated in this block, so their defaults
  # must exist here instead of later near supervisord startup.
  export BROWSER_REMOTE_DEBUGGING_PORT="${BROWSER_REMOTE_DEBUGGING_PORT:-9222}"
  export WEBSOCKET_PROXY_PORT="${WEBSOCKET_PROXY_PORT:-5700}"
  export SANDBOX_SRV_PORT="${SANDBOX_SRV_PORT:-9988}"
  export CODE_SERVER_PORT="${CODE_SERVER_PORT:-8443}"
  export JUPYTER_LAB_PORT="${JUPYTER_LAB_PORT:-8888}"
  export MCP_SERVER_BROWSER_PORT="${MCP_SERVER_BROWSER_PORT:-8100}"

  # Auth configuration selection
  AUTH_CONFIG="/opt/gem/nginx-server-with-auth.conf"
  NO_AUTH_CONFIG="/opt/gem/nginx-server-without-auth.conf"
  ACTIVE_CONFIG="/opt/gem/nginx-server-active.conf"
  TRIMMED_JWT_PUBLIC_KEY="$(echo -n "$JWT_PUBLIC_KEY" | xargs)"
  TRIMMED_API_KEY="$(echo -n "${SANDBOX_API_KEY:-}" | xargs)"
  if [ -n "$TRIMMED_JWT_PUBLIC_KEY" ] || [ -n "$TRIMMED_API_KEY" ]; then
    envsubst '${PUBLIC_PORT} ${AUTH_BACKEND_PORT} ${SANDBOX_SRV_PORT} ${PUBLIC_LISTEN_IPV4} ${PUBLIC_LISTEN_IPV6} ${RUNTIME_LISTEN}' <"$AUTH_CONFIG" >"$ACTIVE_CONFIG"
  else
    envsubst '${PUBLIC_PORT} ${PUBLIC_LISTEN_IPV4} ${PUBLIC_LISTEN_IPV6} ${RUNTIME_LISTEN}' <"$NO_AUTH_CONFIG" >"$ACTIVE_CONFIG"
    # Security warning when no authentication is configured
    if [ "$PUBLIC_LISTEN_IPV4" != "127.0.0.1" ] || [ "$PUBLIC_LISTEN_IPV6" != "[::1]" ]; then
      echo ""
      echo "================================================================"
      echo "  WARNING: SECURITY RISK - NO AUTHENTICATION CONFIGURED"
      echo "================================================================"
      echo "  The sandbox is listening on ${PUBLIC_LISTEN_IPV4}:${PUBLIC_PORT}"
      echo "  without any authentication. Anyone on the network can execute"
      echo "  arbitrary code in this container."
      echo ""
      echo "  To secure your sandbox, set one of the following:"
      echo "    - SANDBOX_API_KEY=<your-secret-key>  (recommended)"
      echo "    - JWT_PUBLIC_KEY=<base64-encoded-public-key>"
      echo ""
      echo "  Or restrict access to localhost only:"
      echo "    - PUBLIC_LISTEN_IPV4=127.0.0.1"
      echo "    - PUBLIC_LISTEN_IPV6=[::1]"
      echo "    - docker run -p 127.0.0.1:8080:8080 ..."
      echo "================================================================"
      echo ""
    fi
  fi

  # Generate nginx configs
  envsubst '${BROWSER_REMOTE_DEBUGGING_PORT} ${NGINX_PROXY_CONNECT_TIMEOUT} ${NGINX_SESSION_IDLE_TIMEOUT}' <"/opt/gem/nginx.legacy.conf" >"/opt/gem/nginx/legacy.conf"
  envsubst '${WEBSOCKET_PROXY_PORT}' <"/opt/gem/nginx.vnc.conf" >"/opt/gem/nginx/vnc.conf"

  # (removed) nginx.python_srv.conf + nginx.gembrowser_compat.conf —
  # both proxied to python-server :9988 which no longer runs.

  if [ -f "/opt/gem/nginx/nginx.opencode.conf" ]; then
    envsubst '${OPENCODE_PORT}' <"/opt/gem/nginx/nginx.opencode.conf" >"/opt/gem/nginx/opencode.conf" && rm -f "/opt/gem/nginx/nginx.opencode.conf"
  fi

  if [ -f "/opt/gem/nginx/nginx.mcp_hub.conf" ]; then
    envsubst '${SANDBOX_SRV_PORT}' <"/opt/gem/nginx/nginx.mcp_hub.conf" >"/opt/gem/nginx/mcp_hub.conf" && rm -f "/opt/gem/nginx/nginx.mcp_hub.conf"

    EXTRA_MCP_SERVERS="${EXTRA_MCP_SERVERS#"${EXTRA_MCP_SERVERS%%[![:space:]]*}"}"
    EXTRA_MCP_SERVERS="${EXTRA_MCP_SERVERS%"${EXTRA_MCP_SERVERS##*[![:space:]]}"}"
    if [ -n "${EXTRA_MCP_SERVERS}" ]; then
      if ! echo "${EXTRA_MCP_SERVERS}" | jq -e '.' > /dev/null 2>&1; then
        echo "ERROR: EXTRA_MCP_SERVERS JSON format invalid" >&2
        exit 1
      fi
      jq --argjson extra "${EXTRA_MCP_SERVERS}" \
         '.mcpServers = ((.mcpServers + $extra) | with_entries(select(.value != null)))' \
         /opt/gem/mcp-hub.json.template > /opt/gem/mcp-hub.json.template.tmp \
      && mv /opt/gem/mcp-hub.json.template.tmp /opt/gem/mcp-hub.json.template
    fi
    envsubst '${SANDBOX_SRV_PORT} ${MCP_SERVER_BROWSER_PORT} ${BROWSER_REMOTE_DEBUGGING_PORT}' < /opt/gem/mcp-hub.json.template > /opt/gem/mcp-hub.json && rm -f /opt/gem/mcp-hub.json.template
  fi

  # v13-fix9.11: only render jupyter_lab.conf when jupyter is enabled.
# DISABLE_JUPYTER=true means python-server will not start a jupyter
# session pool and the /jupyter REST routes are not registered, so the
# nginx proxy would 502 on every /jupyter request.  Skip rendering and
# delete any stale conf from a previous container run.  Mirror logic in
# app/server.py L36-50 + app/api/router.py L29-45.
if [ "${DISABLE_JUPYTER:-false}" != "true" ] && [ -f "/opt/gem/nginx/nginx.jupyter_lab.conf" ]; then
    envsubst '${JUPYTER_LAB_PORT}' <"/opt/gem/nginx/nginx.jupyter_lab.conf" >"/opt/gem/nginx/jupyter_lab.conf" && rm -f "/opt/gem/nginx/nginx.jupyter_lab.conf"
elif [ "${DISABLE_JUPYTER:-false}" = "true" ]; then
    rm -f "/opt/gem/nginx/jupyter_lab.conf" "/opt/gem/nginx/nginx.jupyter_lab.conf" 2>/dev/null || true
    log "v13-fix9.11: DISABLE_JUPYTER=true — nginx /jupyter location removed"
fi
  # code-server removed (v11-no-code build)
  # [ -f "/opt/gem/nginx/nginx.code_server.conf" ] && \
  #   envsubst '${CODE_SERVER_PORT}' <"/opt/gem/nginx/nginx.code_server.conf" >"/opt/gem/nginx/code_server.conf" && rm -f "/opt/gem/nginx/nginx.code_server.conf"
# /browser-ui dashboard panel is controlled independently from the chrome
# process: DISABLE_BROWSER_UI=true (default) removes the nginx location so
# the dashboard never shows the BrowserUI panel, even though chrome/CDP 9222
# stays up for VNC + agent use.
if [ "${DISABLE_BROWSER_UI:-true}" != "true" ] && [ -f "/opt/gem/nginx/nginx.ui_browser.conf" ]; then
    envsubst '${BROWSER_REMOTE_DEBUGGING_PORT} ${NGINX_PROXY_CONNECT_TIMEOUT} ${NGINX_SESSION_IDLE_TIMEOUT}' <"/opt/gem/nginx/nginx.ui_browser.conf" >"/opt/gem/nginx/ui_browser.conf" && rm -f "/opt/gem/nginx/nginx.ui_browser.conf"
elif [ "${DISABLE_BROWSER_UI:-true}" = "true" ]; then
    rm -f "/opt/gem/nginx/ui_browser.conf" "/opt/gem/nginx/nginx.ui_browser.conf" 2>/dev/null || true
    log "DISABLE_BROWSER_UI=true — nginx /browser-ui location removed (chrome still runs)"
fi

# Terminal UI: dashboard panel is hidden (index.html enabled:false); remove the
# /terminal nginx location too so the endpoint cannot be reached directly via
# :8080. Set DISABLE_TERMINAL_UI=false to re-enable the /terminal page.
if [ "${DISABLE_TERMINAL_UI:-true}" != "true" ] && [ -f "/opt/gem/nginx/nginx.ui_terminal.conf" ]; then
    mv "/opt/gem/nginx/nginx.ui_terminal.conf" "/opt/gem/nginx/ui_terminal.conf"
elif [ "${DISABLE_TERMINAL_UI:-true}" = "true" ]; then
    rm -f "/opt/gem/nginx/ui_terminal.conf" "/opt/gem/nginx/nginx.ui_terminal.conf" 2>/dev/null || true
    log "DISABLE_TERMINAL_UI=true — nginx /terminal location removed"
fi

# opencode is disabled by default: drop its /opencode location, which otherwise
# 302-redirects straight into /terminal.
if [ "${DISABLE_OPENCODE:-true}" = "true" ]; then
    rm -f "/opt/gem/nginx/opencode.conf" 2>/dev/null || true
fi

# MCP hub disabled by default: drop /mcp + /v1/mcp routes (502 zombies otherwise).
if [ "${DISABLE_MCP_BROWSER:-true}" = "true" ]; then
    rm -f "/opt/gem/nginx/mcp_hub.conf" 2>/dev/null || true
fi

  envsubst '${PUBLIC_PORT} ${PUBLIC_LISTEN_IPV4} ${PUBLIC_LISTEN_IPV6}' <"/opt/gem/nginx-server-port-proxy.conf.template" >"/opt/gem/nginx-server-port-proxy.conf"

) &
PID_CONFIG=$!

# Wait for both parallel tasks
wait $PID_USER_SETUP || { log "ERROR: user_setup failed"; exit 1; }
wait $PID_CONFIG || { log "ERROR: config setup failed"; exit 1; }
timing_checkpoint "parallel_setup"

# ----------------------
# Environment Variables Setup
# ----------------------
export IMAGE_VERSION=$(cat /etc/aio_version 2>/dev/null || echo "unknown")

# Workspace directory (global for all services)
export WORKSPACE="${WORKSPACE:-/home/$USER}"
mkdir -p "$WORKSPACE"
chown "$USER:$USER" "$WORKSPACE"

# AIO_USER: Trusted user identity for sandboxd integration
# If AIO_USER is set, use it to override USER; otherwise fallback to USER
export AIO_USER="${AIO_USER:-$USER}"
export USER="$AIO_USER"

# Node.js version setup (symlinks already exist, just export the version var)
export NODE_CODE_EXEC_VERSION="${NODE_CODE_EXEC_VERSION:-$NODE_VERSION}"
export NGINX_LOG_LEVEL=${NGINX_LOG_LEVEL:-debug}
# Source system-wide Node.js/fnm env (same config used by interactive shells)
# Override HOME so nodejs.sh resolves paths for the sandbox user, not root
export HOME="/home/$USER"
. /etc/profile.d/nodejs.sh
export HOMEPAGE=${HOMEPAGE:-""}
# Resolve BROWSER_LANG to a locale pak that actually exists (e.g. ja-JP -> ja)
BROWSER_LANG_RESOLVED="${BROWSER_LANG:-en-US}"
# Derive locales from browser binary location (works for both /opt/browser and system installs)
BROWSER_BIN=$(readlink -f /usr/local/bin/browser 2>/dev/null || true)
LOCALES_DIR="$(dirname "${BROWSER_BIN:-/opt/browser/chrome}")/locales"
if [ ! -f "${LOCALES_DIR}/${BROWSER_LANG_RESOLVED}.pak" ]; then
  BROWSER_LANG_BASE="${BROWSER_LANG_RESOLVED%%-*}"
  if [ -f "${LOCALES_DIR}/${BROWSER_LANG_BASE}.pak" ]; then
    BROWSER_LANG_RESOLVED="${BROWSER_LANG_BASE}"
  fi
fi
export BROWSER_EXTRA_ARGS="${BROWSER_NO_SANDBOX} --lang=${BROWSER_LANG_RESOLVED} --time-zone-for-testing=${TZ} ${BROWSER_EXTRA_ARGS}"
# Skip Chrome first-run wizard & sign-in prompt unless BROWSER_NO_FIRST_RUN=false.
# --no-first-run suppresses the onboarding tabs, --no-default-browser-check skips
# the default-browser nag, --disable-sync keeps the profile from offering sign-in.
if [ "${BROWSER_NO_FIRST_RUN:-true}" != "false" ]; then
  export BROWSER_EXTRA_ARGS="${BROWSER_EXTRA_ARGS} --no-first-run --no-default-browser-check --disable-sync"
  log "BROWSER_NO_FIRST_RUN: skipping Chrome first-run wizard & sign-in prompt"
fi
timing_checkpoint "env_vars_setup"

# ----------------------
# HTTP(S) proxy (gost) removed from the final image (no binary, no conf).
# nginx.conf unconditionally includes /opt/gem/nginx-proxy-map.conf, so keep
# an (empty) file there or nginx would fail to start.
# ----------------------
touch /opt/gem/nginx-proxy-map.conf
timing_checkpoint "gost_config"

# ----------------------
# Generate Index Page
# ----------------------
# /opt/aio/index.html is a baked-but-incomplete template: the SANDBOX_CONFIG
# block at the top uses ${DISABLE_JUPYTER} / ${DISABLE_CODE_SERVER} /
# ${DISABLE_BROWSER} / ${DISABLE_OPENCODE} placeholders that need to be
# envsubst'd at container startup with the effective runtime values.
# The "if [ -f ... template ]" branch was originally for a separate file;
# here we rewrite the baked file in place so /browser-ui /code-server /jupyter
# toggle correctly based on DISABLE_* env vars. See commit notes for the
# reason we chose rewrite-in-place over renaming to .template.
if [ -f "/opt/aio/index.html" ]; then
  envsubst '${DISABLE_JUPYTER},${DISABLE_CODE_SERVER},${DISABLE_BROWSER},${DISABLE_OPENCODE}' \
    < /opt/aio/index.html > /opt/aio/index.html.rendered
  mv /opt/aio/index.html.rendered /opt/aio/index.html
fi
timing_checkpoint "index_page"

# ----------------------
# Display Startup Banner
# ----------------------
print_banner() {
  echo ""
  echo -e "\033[36m █████╗ ██╗ ██████╗     ███████╗ █████╗ ███╗   ██╗██████╗ ██████╗  ██████╗ ██╗  ██╗\033[0m"
  echo -e "\033[36m██╔══██╗██║██╔═══██╗    ██╔════╝██╔══██╗████╗  ██║██╔══██╗██╔══██╗██╔═══██╗╚██╗██╔╝\033[0m"
  echo -e "\033[36m███████║██║██║   ██║    ███████╗███████║██╔██╗ ██║██║  ██║██████╔╝██║   ██║ ╚███╔╝\033[0m"
  echo -e "\033[36m██╔══██║██║██║   ██║    ╚════██║██╔══██║██║╚██╗██║██║  ██║██╔══██╗██║   ██║ ██╔██╗\033[0m"
  echo -e "\033[36m██║  ██║██║╚██████╔╝    ███████║██║  ██║██║ ╚████║██████╔╝██████╔╝╚██████╔╝██╔╝ ██╗\033[0m"
  echo -e "\033[36m╚═╝  ╚═╝╚═╝ ╚═════╝     ╚══════╝╚═╝  ╚═╝╚═╝  ╚═══╝╚═════╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═╝\033[0m"
  echo ""
  echo -e "\033[32m AIO(All-in-One) Agent Sandbox Environment\033[0m"
  if [ -n "${IMAGE_VERSION}" ]; then
    echo -e "\033[34m Image Version: ${IMAGE_VERSION}\033[0m"
  fi
  echo -e "\033[33m Dashboard: http://localhost:${PUBLIC_PORT}\033[0m"
  echo -e "\033[33m Documentation: http://localhost:${PUBLIC_PORT}/v1/docs\033[0m"
  echo ""
  echo -e "\033[35m================================================================\033[0m"
}

print_banner
timing_checkpoint "banner"

# ----------------------
# Shutdown Hooks File
# ----------------------
echo "[]" > "/var/lib/aio-sandbox/shutdown-hooks.json"
chown $USER:$USER /var/lib/aio-sandbox/shutdown-hooks.json

# ----------------------
# Run Pre-services Hook
# ----------------------
run_hook "RUN_HOOK_PRE_SERVICES" "$RUN_HOOK_PRE_SERVICES"
timing_checkpoint "pre_services_hook"

# ----------------------
# Post-Ready Hook (one-time, runs in background after services are up)
# This runs independently of supervisord so nginx restarts cannot re-trigger it.
# ----------------------
if [ -n "$RUN_HOOK_POST_READY" ]; then
  (
    _trim_wait_item() {
      local _value="$1"
      _value="${_value#"${_value%%[![:space:]]*}"}"
      _value="${_value%"${_value##*[![:space:]]}"}"
      printf '%s' "$_value"
    }

    _join_wait_items() {
      local _first=1
      local _item
      for _item in "$@"; do
        if [ $_first -eq 1 ]; then
          printf '%s' "$_item"
          _first=0
        else
          printf ',%s' "$_item"
        fi
      done
    }

    # Build the readiness targets to wait for.
    # python-server :9988 (SANDBOX_SRV_PORT) is gone — only wait on
    # browser CDP :9222 by default.
    _hook_ports=()
    _hook_files=()
    if [ -n "$WAIT_PORTS" ]; then
      IFS=',' read -ra _hook_ports <<< "$WAIT_PORTS"
    else
      [ -n "$BROWSER_REMOTE_DEBUGGING_PORT" ] && _hook_ports+=("$BROWSER_REMOTE_DEBUGGING_PORT")
    fi
    if [ -n "$WAIT_FILES" ]; then
      IFS=',' read -ra _hook_files <<< "$WAIT_FILES"
    fi

    _normalized_ports=()
    for _p in "${_hook_ports[@]}"; do
      _p=$(_trim_wait_item "$_p")
      [ -n "$_p" ] && _normalized_ports+=("$_p")
    done
    _hook_ports=("${_normalized_ports[@]}")

    _normalized_files=()
    for _f in "${_hook_files[@]}"; do
      _f=$(_trim_wait_item "$_f")
      [ -n "$_f" ] && _normalized_files+=("$_f")
    done
    _hook_files=("${_normalized_files[@]}")

    _start=$(date +%s)
    _timeout="${WAIT_TIMEOUT:-120}"

    while [ ${#_hook_ports[@]} -gt 0 ] || [ ${#_hook_files[@]} -gt 0 ]; do
      _pending_ports=()
      for _p in "${_hook_ports[@]}"; do
        nc -z localhost "$_p" >/dev/null 2>&1 || _pending_ports+=("$_p")
      done
      _hook_ports=("${_pending_ports[@]}")

      _pending_files=()
      for _f in "${_hook_files[@]}"; do
        [ -f "$_f" ] || _pending_files+=("$_f")
      done
      _hook_files=("${_pending_files[@]}")

      [ ${#_hook_ports[@]} -eq 0 ] && [ ${#_hook_files[@]} -eq 0 ] && break
      if [ $(( $(date +%s) - _start )) -ge "$_timeout" ]; then
        log "Skipping RUN_HOOK_POST_READY because readiness wait timed out after ${_timeout}s."
        [ ${#_hook_ports[@]} -gt 0 ] && log "Pending post-ready ports: $(_join_wait_items "${_hook_ports[@]}")"
        [ ${#_hook_files[@]} -gt 0 ] && log "Pending post-ready files: $(_join_wait_items "${_hook_files[@]}")"
        exit 0
      fi
      sleep 1
    done

    run_hook "RUN_HOOK_POST_READY" "$RUN_HOOK_POST_READY"
  ) &
fi

# ----------------------
# Start Supervisord
# ----------------------
log "Starting supervisord as the main process..."
timing_checkpoint "supervisord_start"
# Default X11 display for the VNC-based workflow; can be overridden by env.
export DISPLAY="${DISPLAY:-:99}"
export TZ="${TZ:-UTC}"
# Comprehensive defaults for supervisord config interpolations.
# Many supervisord/*.conf use %(ENV_X)s placeholders; we ensure each
# is defined even when the build-time ENV_* didn't reach the runtime.
export DISPLAY_DEPTH="${DISPLAY_DEPTH:-24}"
export DISPLAY_WIDTH="${DISPLAY_WIDTH:-1920}"
export DISPLAY_HEIGHT="${DISPLAY_HEIGHT:-1080}"
export VNC_SERVER_PORT="${VNC_SERVER_PORT:-5900}"
export BROWSER_REMOTE_DEBUGGING_PORT="${BROWSER_REMOTE_DEBUGGING_PORT:-9222}"
export WEBSOCKET_PROXY_PORT="${WEBSOCKET_PROXY_PORT:-5700}"
export AUTOSTART_VNC="${AUTOSTART_VNC:-true}"
export AUTOSTART_CODE_SERVER="${AUTOSTART_CODE_SERVER:-true}"
export AUTOSTART_BROWSER="${AUTOSTART_BROWSER:-true}"
export AUTOSTART_JUPYTER="${AUTOSTART_JUPYTER:-true}"
export AUTOSTART_CJK_IME="${AUTOSTART_CJK_IME:-true}"
export AUTOSTART_MCP_BROWSER="${AUTOSTART_MCP_BROWSER:-true}"
export AUTOSTART_NODEJS_REPL_20="${AUTOSTART_NODEJS_REPL_20:-true}"
export AUTOSTART_NODEJS_REPL_22="${AUTOSTART_NODEJS_REPL_22:-true}"
export AUTOSTART_NODEJS_REPL_24="${AUTOSTART_NODEJS_REPL_24:-true}"
export AIO_DBUS_SESSION_SOCKET="${AIO_DBUS_SESSION_SOCKET:-/run/dbus/aio-dbus-session-socket}"
export AIO_USER="${AIO_USER:-$USER}"
export BRWSR_EXTRA_ARGS="${BRWSR_EXTRA_ARGS:-}"
export NODE_VERSION="${NODE_VERSION:-22}"
export NODE_CODE_EXEC_VERSION="${NODE_CODE_EXEC_VERSION:-22}"
export IMAGE_VERSION="${IMAGE_VERSION:-unknown}"
export HOMEPAGE="${HOMEPAGE:-/opt/aio/index.html}"
export SANDBOX_SRV_PORT="${SANDBOX_SRV_PORT:-9988}"
export CODE_SERVER_PORT="${CODE_SERVER_PORT:-8443}"
export JUPYTER_LAB_PORT="${JUPYTER_LAB_PORT:-8888}"
export MCP_SERVER_BROWSER_PORT="${MCP_SERVER_BROWSER_PORT:-8100}"
export JUPYTER_PLATFORM_DIRS="${JUPYTER_PLATFORM_DIRS:-1}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore}"
export XAUTHORITY="${XAUTHORITY:-/tmp/.X11-unix/Xauthority}"

# ----------------------
# Prepare PM2_HOME before supervisord starts. sandbox-pm2-runtime is the sole
# PM2 daemon owner in the rootless image and runs as user x, so it creates
# rpc.sock/pub.sock with the correct ownership without a separate pre-warm.
# ----------------------
if [ -n "${USER:-}" ] && [ "${USER}" != "root" ]; then
  mkdir -p /home/${USER}/.pm2
  chown ${USER_UID:-1000}:${USER_GID:-1000} /home/${USER}/.pm2
  chmod 755 /home/${USER}/.pm2
fi

# ----------------------
# Make container stdout/stderr pipes world-writable for the non-root
# supervisor process. When launched as root (default `docker run`), docker
# creates the stdout pipe owned by root (0600). supervisord drops to user=x
# via `[supervisord] user=x`, then opens /dev/stdout (-> /proc/self/fd/1)
# for each `[program:...] stdout_logfile=/dev/stdout`; as uid 1000 it gets
# EACCES because the pipe is root-owned. fchmod 0666 lets uid-1000 open the
# same pipe. Under k8s runAsUser:1000 the pipe is already owned by 1000, so
# this block is a no-op (and uid-1000 couldn't fchmod a root pipe anyway).
# ----------------------
if [ "$(id -u)" = "0" ]; then
  python3 -c "import os; os.fchmod(1, 0o666); os.fchmod(2, 0o666)" 2>/dev/null || true
fi

exec /usr/bin/supervisord -n -c /opt/gem/supervisord.conf
