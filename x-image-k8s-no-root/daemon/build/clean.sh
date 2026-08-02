#!/bin/bash
#######################################################################
# 清理构建产物
#######################################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_NEW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[INFO] 清理构建产物..."

# 清理二进制和插件
rm -rf "$DAEMON_NEW_DIR/bin"
rm -rf "$DAEMON_NEW_DIR/dist"

# 清理 tar 文件
rm -f "$DAEMON_NEW_DIR"/*.tar

# 清理 Docker 镜像
if [ -n "$(docker images -q daytona-new:amd64 2>/dev/null)" ]; then
    echo "[INFO] 删除 Docker 镜像: daytona-new:amd64"
    docker rmi -f daytona-new:amd64 2>/dev/null || true
fi

# 重建目录
mkdir -p "$DAEMON_NEW_DIR/bin"
mkdir -p "$DAEMON_NEW_DIR/dist/libs"

echo "[INFO] ✓ 清理完成"
