#!/bin/bash
#######################################################################
# Daytona Daemon-New 完整构建流程
#
# 用途：编译 daemon 二进制 + 准备依赖文件 + 构建 Docker 镜像 + 保存 tar
#
# 流程：
#   1. 编译 daemon amd64 二进制（yarn nx build-amd64 daemon）
#   2. 复制 computer-use 插件（从项目根 dist/libs）
#   3. 构建 Docker 镜像
#   4. 保存为 tar 文件
#
# 使用：
#   ./build/build.sh                  # 完整流程
#   ./build/build.sh --skip-daemon    # 跳过 daemon 编译
#   ./build/build.sh --skip-image     # 跳过镜像构建
#   ./build/build.sh --clean          # 清理后再构建
#######################################################################

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 路径定义
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DAEMON_NEW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_ROOT="$(cd "$DAEMON_NEW_DIR/.." && pwd)"

# 产物路径
DAEMON_BIN_SRC="$PROJECT_ROOT/dist/apps/daemon-amd64/daytona"
DAEMON_BIN_DST="$DAEMON_NEW_DIR/bin/daytona"

COMPUTER_USE_SRC="$PROJECT_ROOT/dist/libs/computer-use-amd64"
COMPUTER_USE_DST="$DAEMON_NEW_DIR/dist/libs/computer-use-amd64"

# 镜像配置
IMAGE_NAME="${IMAGE_NAME:-daytona-new}"
IMAGE_TAG="${IMAGE_TAG:-amd64}"
IMAGE_TAR="${IMAGE_TAR:-$DAEMON_NEW_DIR/daytona-new-amd64.tar}"

# 解析参数
SKIP_DAEMON=false
SKIP_IMAGE=false
CLEAN=false

for arg in "$@"; do
    case $arg in
        --skip-daemon)
            SKIP_DAEMON=true
            ;;
        --skip-image)
            SKIP_IMAGE=true
            ;;
        --clean)
            CLEAN=true
            ;;
        --help|-h)
            echo "Usage: $0 [--skip-daemon] [--skip-image] [--clean]"
            exit 0
            ;;
    esac
done

log_info "========================================="
log_info "Daytona Daemon-New 完整构建流程"
log_info "========================================="
log_info "项目根目录: $PROJECT_ROOT"
log_info "daemon-new 目录: $DAEMON_NEW_DIR"
log_info "镜像名称: ${IMAGE_NAME}:${IMAGE_TAG}"
log_info "========================================="

#######################################################################
# 步骤 0：清理（可选）
#######################################################################
if [ "$CLEAN" = true ]; then
    log_info "[Step 0] 清理旧的构建产物..."
    rm -rf "$DAEMON_NEW_DIR/bin"
    rm -rf "$DAEMON_NEW_DIR/dist"
    mkdir -p "$DAEMON_NEW_DIR/bin"
    mkdir -p "$DAEMON_NEW_DIR/dist/libs"
fi

#######################################################################
# 步骤 1：编译 daemon amd64 二进制
#######################################################################
if [ "$SKIP_DAEMON" = false ]; then
    log_info "[Step 1/4] 编译 daemon amd64 二进制..."

    cd "$PROJECT_ROOT"

    if [ ! -d "node_modules" ]; then
        log_warn "未检测到 node_modules，先执行 yarn install"
        yarn install
    fi

    log_info "执行: yarn nx build-amd64 daemon"
    yarn nx build-amd64 daemon

    if [ ! -f "$DAEMON_BIN_SRC" ]; then
        log_error "daemon 二进制未生成: $DAEMON_BIN_SRC"
        exit 1
    fi

    mkdir -p "$DAEMON_NEW_DIR/bin"
    cp -f "$DAEMON_BIN_SRC" "$DAEMON_BIN_DST"
    chmod +x "$DAEMON_BIN_DST"

    log_info "✓ daemon 二进制已生成: $DAEMON_BIN_DST"
    log_info "  大小: $(ls -lh "$DAEMON_BIN_DST" | awk '{print $5}')"
else
    log_warn "[Step 1/4] 跳过 daemon 编译（--skip-daemon）"
    if [ ! -f "$DAEMON_BIN_DST" ]; then
        log_error "daemon 二进制不存在: $DAEMON_BIN_DST"
        log_error "请先去掉 --skip-daemon 选项"
        exit 1
    fi
fi

#######################################################################
# 步骤 2：复制 computer-use 插件
#######################################################################
log_info "[Step 2/4] 复制 computer-use 插件..."

if [ ! -f "$COMPUTER_USE_SRC" ]; then
    log_warn "computer-use 插件不存在: $COMPUTER_USE_SRC"
    log_warn "正在尝试执行项目自带的构建脚本..."

    cd "$PROJECT_ROOT"
    if [ -f "hack/computer-use/build-computer-use-amd64.sh" ]; then
        ./hack/computer-use/build-computer-use-amd64.sh
    else
        log_error "未找到 computer-use 构建脚本"
        exit 1
    fi
fi

if [ ! -f "$COMPUTER_USE_SRC" ]; then
    log_error "computer-use 插件仍未生成: $COMPUTER_USE_SRC"
    exit 1
fi

mkdir -p "$DAEMON_NEW_DIR/dist/libs"
cp -f "$COMPUTER_USE_SRC" "$COMPUTER_USE_DST"
chmod 755 "$COMPUTER_USE_DST"

log_info "✓ computer-use 已复制: $COMPUTER_USE_DST"
log_info "  大小: $(ls -lh "$COMPUTER_USE_DST" | awk '{print $5}')"

#######################################################################
# 步骤 3：构建 Docker 镜像
#######################################################################
if [ "$SKIP_IMAGE" = false ]; then
    log_info "[Step 3/4] 构建 Docker 镜像..."

    cd "$DAEMON_NEW_DIR"

    docker buildx build \
        --platform linux/amd64 \
        -t "${IMAGE_NAME}:${IMAGE_TAG}" \
        --load \
        -f Dockerfile \
        .

    log_info "✓ 镜像构建完成: ${IMAGE_NAME}:${IMAGE_TAG}"
else
    log_warn "[Step 3/4] 跳过 Docker 镜像构建（--skip-image）"
fi

#######################################################################
# 步骤 4：保存为 tar
#######################################################################
if [ "$SKIP_IMAGE" = false ]; then
    log_info "[Step 4/4] 保存镜像为 tar 文件..."

    docker save -o "$IMAGE_TAR" "${IMAGE_NAME}:${IMAGE_TAG}"

    log_info "✓ 镜像 tar 已保存: $IMAGE_TAR"
    log_info "  大小: $(ls -lh "$IMAGE_TAR" | awk '{print $5}')"
else
    log_warn "[Step 4/4] 跳过 tar 导出（--skip-image）"
fi

log_info "========================================="
log_info "构建完成！"
log_info "========================================="
log_info "产物列表："
log_info "  - daemon 二进制: $DAEMON_BIN_DST"
log_info "  - computer-use 插件: $COMPUTER_USE_DST"
log_info "  - Docker 镜像: ${IMAGE_NAME}:${IMAGE_TAG}"
if [ "$SKIP_IMAGE" = false ]; then
    log_info "  - 镜像 tar: $IMAGE_TAR"
fi
log_info "========================================="
log_info ""
log_info "后续部署命令："
log_info "  docker load -i $IMAGE_TAR"
log_info "  docker run -d --name daytona-new -p 8080:8080 ${IMAGE_NAME}:${IMAGE_TAG}"
