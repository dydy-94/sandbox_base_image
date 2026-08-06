#!/bin/bash
# Thin wrapper — canonical entrypoint is /opt/application/run.sh
exec /opt/application/run.sh "$@"
