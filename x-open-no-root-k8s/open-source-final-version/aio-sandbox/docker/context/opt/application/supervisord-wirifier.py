#!/usr/bin/env python3
"""supervisord-wirifier.py — applies all the build-time supervisord env fixes
and writes them via /opt/gem/supervisord/*.

Idempotent: detects already-patched files via the `CODE_SERVER_PORT=` marker.
"""
import os
SUFFIX = "CODE_SERVER_PORT=8443,JUPYTER_LAB_PORT=8888,SANDBOX_SRV_PORT=9988"
TARGETS = [
    ("/opt/gem/supervisord/supervisord.code_server.conf",
     'VSCODE_PROXY_URI="https://{{port}}-{{host}}"'),
    ("/opt/gem/supervisord/supervisord.jupyter.conf",
     'JUPYTER_DATA_DIR="/opt/jupyter/data"'),
    ("/opt/gem/supervisord/supervisord.python_srv.conf",
     'PYTHONWARNINGS="ignore"'),
]
for path, _ in TARGETS:
    if not os.path.exists(path):
        continue
    src = open(path).read().splitlines()
    out = []
    for line in src:
        if line.startswith("environment=") and "CODE_SERVER_PORT=" not in line:
            idx = line.rfind(chr(34))
            if idx != -1:
                line = line[:idx] + "," + SUFFIX + line[idx:]
        out.append(line)
    open(path, "w").write("\n".join(out) + "\n")
    print("patched", path)

# Rewrite python-server wrapper with hard-coded PYTHONPATH
wrapper = "/opt/gem/supervisord/python_srv_wrapper.sh"
if os.path.exists(wrapper):
    new = ('#!/bin/bash\n'
           'export PYTHONPATH="/opt/server-venv/lib/python3/site-packages${PYTHONPATH:+:${PYTHONPATH}}"\n'
           'echo "Starting python-server with PYTHONPATH=${PYTHONPATH}"\n'
           'exec /usr/local/bin/python-server "$@"\n')
    open(wrapper, "w").write(new)
    os.chmod(wrapper, 0o755)
    print("rewrote", wrapper)

# Default port expansion for *.sh scripts
for script, var, default in [
    ("/opt/gem/jupyter-lab.sh", "JUPYTER_LAB_PORT", "8888"),
    ("/opt/gem/code-server.sh", "CODE_SERVER_PORT", "8443"),
]:
    if not os.path.exists(script):
        continue
    s = open(script).read()
    s = s.replace("${" + var + "}", "${" + var + ":-" + default + "}")
    open(script, "w").write(s)
    print("applied", var, "fallback in", script)

import subprocess
subprocess.run(["mkdir", "-p", "/home/x/.config/code-server"], check=False)
subprocess.run(["chown", "-R", "1000:1000", "/home/x"], check=False)
print("home/x permissions fixed")
