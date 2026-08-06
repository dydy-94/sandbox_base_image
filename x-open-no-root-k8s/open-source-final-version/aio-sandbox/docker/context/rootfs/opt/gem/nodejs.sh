#!/bin/bash
# Lightweight nodejs.sh stub for offline builds.
#
# The upstream aio-sandbox Dockerfile ships `/opt/gem/nodejs.sh` that
# activates fnm + node in any login shell. The upstream source isn't
# publicly available, so we replicate the minimum required behaviour
# so `run.sh`'s `. /etc/profile.d/nodejs.sh` doesn't fail.

export FNM_PATH=/opt/fnm
if [ -x "$FNM_PATH/fnm" ]; then
    eval "$("$FNM_PATH/fnm" env --shell bash --use-on-cd 2>/dev/null || "$FNM_PATH/fnm" env 2>/dev/null)" 2>/dev/null || true
    export PATH="$FNM_PATH/aliases/default/bin:$PATH"
fi

# Fallback: put /usr/local/bin first so `node`, `npm`, `npx` resolve.
export PATH="/usr/local/bin:$PATH"

# If $HOME doesn't have an fnm multishell symlink, create one so the
# runtime check in aio_cli's `node` resolution works.
if [ -n "$HOME" ] && [ -d "$HOME" ]; then
    mkdir -p "$HOME/.fnm"
    ln -sfn /opt/fnm/aliases/default/bin "$HOME/.fnm_multishell" 2>/dev/null || true
fi
