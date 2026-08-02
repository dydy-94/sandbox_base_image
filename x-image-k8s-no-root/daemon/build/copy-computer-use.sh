#!/bin/bash
#######################################################################
# 复制 computer-use 插件到 daemon-new/dist/libs/
#
# 来源：项目根目录的 dist/libs/computer-use-amd64
# 目标：daemon-new/dist/libs/computer-use-amd64
#
# 如果项目根目录下不存在该文件，会调用项目自带的构建脚本：
#   hack/computer-use/build-computer-use-amd64.sh
#######################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_NEW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$DAEMON_NEW_DIR/.." && pwd)"

COMPUTER_USE_SRC="$PROJECT_ROOT/dist/libs/computer-use-amd64"
COMPUTER_USE_DST="$DAEMON_NEW_DIR/dist/libs/computer-use-amd64"

if [ ! -f "$COMPUTER_USE_SRC" ]; then
    echo "[WARN] 未找到: $COMPUTER_USE_SRC"
    echo "[INFO] 调用项目自带的构建脚本..."

    cd "$PROJECT_ROOT"
    if [ ! -f "hack/computer-use/build-computer-use-amd64.sh" ]; then
        echo "[ERROR] 构建脚本不存在: hack/computer-use/build-computer-use-amd64.sh"
        exit 1
    fi

    ./hack/computer-use/build-computer-use-amd64.sh
fi

if [ ! -f "$COMPUTER_USE_SRC" ]; then
    echo "[ERROR] computer-use 二进制未生成: $COMPUTER_USE_SRC"
    exit 1
fi

mkdir -p "$DAEMON_NEW_DIR/dist/libs"
cp -f "$COMPUTER_USE_SRC" "$COMPUTER_USE_DST"
chmod 755 "$COMPUTER_USE_DST"

echo "[INFO] ✓ computer-use 已复制: $COMPUTER_USE_DST"
echo "[INFO]   大小: $(ls -lh "$COMPUTER_USE_DST" | awk '{print $5}')"
