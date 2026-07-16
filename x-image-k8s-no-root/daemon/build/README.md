# Daytona Daemon-New 构建流程

本目录统一管理 `daemon-new` 镜像的整个构建流程。

## 目录结构

```
daemon-new/
├── build/                      # 构建流程管理（当前目录）
│   ├── build.sh                # 主构建脚本（一键完成）
│   ├── build-daemon.sh         # 单独编译 daemon
│   ├── copy-computer-use.sh    # 单独复制 computer-use
│   ├── build-image.sh          # 单独构建镜像
│   ├── clean.sh                # 清理脚本
│   └── README.md               # 本文档
├── bin/
│   └── daytona                 # daemon 二进制（编译后生成）
├── dist/
│   └── libs/
│       └── computer-use-amd64  # computer-use 插件（复制后生成）
├── Dockerfile                  # Docker 镜像构建文件
└── ...                         # 源代码
```

## 快速开始

### 方式一：一键完整构建（推荐）

```bash
cd /Users/daidai/daytona_cmb/daemon-new
./build/build.sh
```

### 方式二：分步执行

```bash
cd /Users/daidai/daytona_cmb/daemon-new

# 1. 编译 daemon 二进制
./build/build-daemon.sh

# 2. 复制 computer-use 插件
./build/copy-computer-use.sh

# 3. 构建 Docker 镜像
./build/build-image.sh
```

## 参数说明

| 参数 | 说明 |
|------|------|
| `--skip-daemon` | 跳过 daemon 编译 |
| `--skip-image` | 跳过 Docker 镜像构建 |
| `--clean` | 清理后再构建 |
| `--help` | 显示帮助 |

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `IMAGE_NAME` | `daytona-new` | 镜像名称 |
| `IMAGE_TAG` | `amd64` | 镜像 tag |
| `IMAGE_TAR` | `$DAEMON_NEW_DIR/daytona-new-amd64.tar` | tar 文件路径 |

### 自定义示例

```bash
# 自定义镜像名
IMAGE_NAME=my-daytona IMAGE_TAG=v1.0 ./build/build.sh

# 自定义 tar 路径
IMAGE_TAR=/tmp/my-daytona.tar ./build/build.sh
```

## 前置条件

| 工具 | 版本 | 用途 |
|------|------|------|
| Node.js | 22+ | Nx 构建工具 |
| Yarn | 最新 | 依赖管理 |
| Go | 1.23+ | 由 Nx 调用 |
| Docker | 20+ | 镜像构建 |

## 验证构建

```bash
# 检查产物
ls -la bin/daytona dist/libs/computer-use-amd64

# 检查镜像
docker images | grep daytona-new

# 检查 tar
ls -la *.tar

# 测试运行
docker run -d --name test-daytona -p 8080:8080 daytona-new:amd64
docker logs test-daytona
docker rm -f test-daytona
```

## 部署

```bash
# 加载镜像
docker load -i daytona-new-amd64.tar

# 启动容器
docker run -d --name daytona-new \
  -p 8080:8080 \
  -p 2280:2280 \
  -p 22222:22222 \
  -p 22220:22220 \
  daytona-new:amd64
```

## 常见问题

### Q1: 编译失败 "go: command not found"

Nx 需要 Go 1.23+，请确保 `go version` 输出正确。

### Q2: 提示 "computer-use 插件不存在"

脚本会自动调用 `hack/computer-use/build-computer-use-amd64.sh`，请确保该脚本可执行。

### Q3: 镜像构建失败

检查：
- 基础镜像是否可访问
- `bin/daytona` 是否存在且可执行
- `dist/libs/computer-use-amd64` 是否存在

### Q4: 清理后无法重建

执行：
```bash
./build/clean.sh
./build/build.sh
```

## 详细流程说明

### Step 1: 编译 daemon

```bash
cd $PROJECT_ROOT  # 项目根目录
yarn nx build-amd64 daemon
# 产物：$PROJECT_ROOT/dist/apps/daemon-amd64/daytona
```

调用 Nx 执行 `build-amd64` target，配置在 `apps/daemon/project.json`：
- `GOARCH=amd64`
- `GOOS=linux`
- `CGO_ENABLED=0`
- `outputPath=dist/apps/daemon-amd64`

### Step 2: 复制 computer-use

来源：`$PROJECT_ROOT/dist/libs/computer-use-amd64`
（如果不存在，会自动调用 `hack/computer-use/build-computer-use-amd64.sh`）

### Step 3: 构建 Docker 镜像

- 基于 `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest`
- COPY `bin/daytona` → `/usr/local/bin/daytona`
- COPY `dist/libs/computer-use-amd64` → `/usr/local/lib/daytona-computer-use/daytona-computer-use`
- 注册到 supervisord 自动启动

### Step 4: 保存 tar

将镜像保存为 tar 文件，便于传输和部署。
