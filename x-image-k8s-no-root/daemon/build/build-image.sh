#!/bin/bash
#######################################################################
# 构建 Docker 镜像并保存为 tar
#
# 流程：
#   1. 检查 daemon 二进制和 computer-use 插件是否存在
#   2. 执行 docker buildx build
#   3. 保存为 tar 文件
#######################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_NEW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DAEMON_BIN="$DAEMON_NEW_DIR/bin/daytona"
COMPUTER_USE="$DAEMON_NEW_DIR/dist/libs/computer-use-amd64"

# 镜像配置
IMAGE_NAME="${IMAGE_NAME:-daytona-new}"
IMAGE_TAG="${IMAGE_TAG:-amd64}"
IMAGE_TAR="${IMAGE_TAR:-$DAEMON_NEW_DIR/daytona-new-amd64.tar}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 前置检查
log_info "检查构建依赖..."

if [ ! -f "$DAEMON_BIN" ]; then
    log_error "daemon 二进制不存在: $DAEMON_BIN"
    log_error "请先执行: ./build/build-daemon.sh"
    exit 1
fi

if [ ! -f "$COMPUTER_USE" ]; then
    log_error "computer-use 插件不存在: $COMPUTER_USE"
    log_error "请先执行: ./build/copy-computer-use.sh"
    exit 1
fi

log_info "✓ 所有依赖已就绪"

# 构建镜像
log_info "构建 Docker 镜像: ${IMAGE_NAME}:${IMAGE_TAG}"
cd "$DAEMON_NEW_DIR"

docker buildx build \
    --platform linux/amd64 \
    -t "${IMAGE_NAME}:${IMAGE_TAG}" \
    --load \
    -f Dockerfile \
    .

log_info "✓ 镜像构建完成: ${IMAGE_NAME}:${IMAGE_TAG}"

# 保存为 tar
log_info "保存镜像为 tar..."
docker save -o "$IMAGE_TAR" "${IMAGE_NAME}:${IMAGE_TAG}"

log_info "✓ 镜像 tar 已保存: $IMAGE_TAR"
log_info "  大小: $(ls -lh "$IMAGE_TAR" | awk '{print $5}')"

log_info "========================================="
log_info "构建完成！"
log_info "========================================="
log_info "后续部署命令："
log_info "  docker load -i $IMAGE_TAR"
log_info "  docker run -d --name daytona-new -p 8080:8080 ${IMAGE_NAME}:${IMAGE_TAG}"
