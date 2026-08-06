#!/bin/bash
set -e
cd /opt/code-server
tar -xzf code-server-4.96.4-linux-amd64.tar.gz
# Two symlinks: /usr/bin/code-server (used by /opt/gem/code-server.sh) and
# /usr/local/bin/code-server (used by anyone reading PATH).
ln -sf /opt/code-server/code-server-4.96.4-linux-amd64/bin/code-server /usr/bin/code-server
ln -sf /opt/code-server/code-server-4.96.4-linux-amd64/bin/code-server /usr/local/bin/code-server
chmod +x /opt/code-server/code-server-4.96.4-linux-amd64/bin/code-server
echo "code-server installed"
