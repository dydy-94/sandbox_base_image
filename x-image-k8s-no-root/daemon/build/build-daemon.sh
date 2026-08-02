#!/bin/bash
#######################################################################
# 编译 daemon amd64 二进制
#
# 此脚本会调用 yarn nx build-amd64 daemon，输出到：
#   $PROJECT_ROOT/dist/apps/daemon-amd64/daytona
#######################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_NEW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$DAEMON_NEW_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

echo "[INFO] 检查 node_modules..."
if [ ! -d "node_modules" ]; then
    echo "[INFO] 执行 yarn install..."
    yarn install
fi

echo "[INFO] 执行: yarn nx build-amd64 daemon"
yarn nx build-amd64 daemon

DAEMON_BIN="$PROJECT_ROOT/dist/apps/daemon-amd64/daytona"
if [ ! -f "$DAEMON_BIN" ]; then
    echo "[ERROR] 编译失败，未生成: $DAEMON_BIN"
    exit 1
fi

echo "[INFO] ✓ daemon 编译成功"
echo "[INFO]   路径: $DAEMON_BIN"
echo "[INFO]   大小: $(ls -lh "$DAEMON_BIN" | awk '{print $5}')"
