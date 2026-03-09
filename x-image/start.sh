#!/bin/bash

# 确保环境变量指向 x 用户
export HOME=/home/x
export USER=x

# 更新 Code-Server 配置文件
cat > /home/x/.config/code-server/config.yaml << CONFIG
bind-addr: 0.0.0.0:8080
auth: none
cert: false
disable-telemetry: true
disable-update-check: true
user-data-dir: /home/x/.local/share/code-server
CONFIG

echo "========================================"
echo "以用户 x 启动服务"
echo "========================================"

# 启动 Viewer、 code server
pm2 start bash --name "claude-code-viewer" -- -c 'export PORT=3100; export HOSTNAME=0.0.0.0; claude-code-viewer'
pm2 start bash --name "code-server" -- -c '/usr/lib/code-server/bin/code-server --config /home/x/.config/code-server/config.yaml'
pm2 save
pm2 startup

# pm2 作为容器住进程
exec pm2 logs --format

