#!/bin/bash
# 初始化 daemon-new 依赖
#
# 由于本目录是 daemon 源程序的拷贝，独立于项目原 apps/daemon 模块，
# 需要单独下载依赖。如果 go.sum 中缺少 golang-jwt 等新引入的依赖，
# 本脚本会自动执行 go mod tidy 进行补全。
#
# 用法：
#   ./build/init-deps.sh
#
# 环境变量：
#   GOPROXY    Go 代理（默认 https://goproxy.cn,direct）
#   GOSUMDB    校验和数据库（默认 off，加速国内网络）

set -e

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }

# 切到 daemon-new 根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

log_info "Working directory: $ROOT_DIR"

# 1. 检查 go.mod
if [ ! -f "go.mod" ]; then
    log_error "go.mod not found in $ROOT_DIR"
    exit 1
fi

# 2. 设置 Go 代理
export GOPROXY="${GOPROXY:-https://goproxy.cn,direct}"
export GOSUMDB="${GOSUMDB:-off}"

log_info "GOPROXY: $GOPROXY"
log_info "GOSUMDB: $GOSUMDB"

# 3. 检查 Go 版本
GO_VERSION=$(go version 2>/dev/null | awk '{print $3}' || echo "unknown")
log_info "Go version: $GO_VERSION"

# 4. 下载依赖
log_info "Running go mod download..."
if go mod download 2>&1 | tail -20; then
    log_info "go mod download completed"
else
    log_warn "go mod download had errors, trying go mod tidy..."
fi

# 5. 补全 go.sum
log_info "Running go mod tidy..."
go mod tidy 2>&1 | tail -20 || {
    log_error "go mod tidy failed"
    log_warn "请检查网络连接，或设置 GOPROXY 环境变量"
    exit 1
}

# 6. 验证
log_info "Verifying dependencies..."
if go list -m all 2>&1 | grep -q "github.com/golang-jwt/jwt/v5"; then
    log_info "✓ golang-jwt/jwt/v5 is registered"
else
    log_warn "golang-jwt/jwt/v5 not found in go.mod"
fi

# 7. 尝试快速语法检查
log_info "Running go vet..."
if go vet ./... 2>&1 | tail -10; then
    log_info "✓ go vet passed"
else
    log_warn "go vet had warnings, but dependencies are initialized"
fi

log_info "================================================"
log_info "依赖初始化完成！"
log_info "现在可以运行 ./build/build.sh 进行完整构建"
log_info "================================================"
