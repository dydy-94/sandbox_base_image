#!/bin/bash

# Keep the packaged app importable regardless of how supervisord passes env.
srv_pythonpath="/opt/server-venv/lib/python3/site-packages"
if [ -d "/opt/server-venv/lib/python3.14/site-packages" ]; then
    srv_pythonpath="${srv_pythonpath}:/opt/server-venv/lib/python3.14/site-packages"
fi
if [ -n "${SRV_PYTHONPATH}" ]; then
    srv_pythonpath="${SRV_PYTHONPATH}:${srv_pythonpath}"
fi
export PYTHONPATH="${srv_pythonpath}${PYTHONPATH:+:${PYTHONPATH}}"

echo "Starting python-server with PYTHONPATH=${PYTHONPATH}"

# Execute python-server with all arguments
exec /usr/local/bin/python-server "$@"
