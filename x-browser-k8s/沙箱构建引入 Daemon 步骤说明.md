# 沙箱构建引入 Daemon 步骤说明

## 一、构建镜像

### 1.1 前提条件

**环境要求**：
| 环境 | 版本要求 |
|------|----------|
| Go | >= 1.21 |
| Docker | >= 20.10 |
| Docker Buildx | 已启用 |

**构建产物要求**：

| 二进制文件 | 输出位置 |
|------------|----------|
| daytona | `bin/daytona` |
| computer-use | `dist/libs/computer-use-amd64` |

构建命令参考 **第三章：构建过程说明**

### 1.2 构建命令

```bash
cd ${PROJECT_DIR}

# 确保两个二进制文件存在
ls -la bin/daytona
ls -la dist/libs/computer-use-amd64

# 构建镜像
docker buildx build --platform linux/amd64 -t x-browser:0.1.1 --load .
```

### 1.3 运行容器

```bash
docker run -d --name test-sandbox \
    -p 8080:8080 \
    -p 2280:2280 \
    -p 22222:22222 \
    x-browser:0.1.1

sleep 5
```

---

## 二、镜像构建说明

### 2.1 架构概述

原始架构中，daemon 进程由 Runner 在容器启动后通过 `docker exec` 注入。修改后的方案将 daemon 二进制直接打包到 Dockerfile 中，由 supervisord 自动启动。

**原始架构**：
```
Base Image (all-in-one-sandbox)
  └── supervisord (PID 1)
       ├── nginx (端口 8080)
       ├── code-server
       └── ...

Runner 注入:
  └── runner 复制 daemon 二进制到容器
  └── runner 启动 daemon 进程 (端口 2280, 22222)
```

**修改后架构**：
```
Base Image (all-in-one-sandbox)
  └── supervisord (PID 1)
       ├── nginx (端口 8080)
       ├── code-server
       └── daytona daemon (端口 2280, 22222) ← 直接由 supervisord 启动
```

### 2.2 进程树

```
supervisord (PID 1, 由 docker-init 启动)
├── nginx (端口 8080)
├── daytona (端口 2280, 22222)
│   └── python3 /tmp/daytona_repl_worker.py
├── code-server
└── jupyter
```

### 2.3 关键点

| 方面 | 说明 |
|------|------|
| **ENTRYPOINT** | 不覆盖，让 supervisord 作为 PID 1 |
| **进程管理** | supervisord 自动启动和重启 daemon |
| **环境变量** | Sandbox 特定变量由 Runner 注入 |

---

## 三、构建过程说明

### 3.1 构建 daytona 主程序

**输入**：
| 项目 | 说明 |
|------|------|
| 源码目录 | `${PROJECT_DIR}/apps/daemon/` |
| 入口文件 | `${PROJECT_DIR}/apps/daemon/cmd/daemon/main.go` |

**构建命令**：
```bash
cd ${PROJECT_DIR}/apps/daemon
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
    -ldflags="-s -w -extldflags=-static" \
    -o ../../bin/daytona \
    ./cmd/daemon

chmod +x ../../bin/daytona
```

**输出**：
| 项目 | 说明 |
|------|------|
| 输出文件 | `${PROJECT_DIR}/bin/daytona` |
| 文件格式 | ELF 64-bit LSB executable, statically linked |
| 架构 | Linux amd64 |
| 特性 | 静态链接，无动态依赖 |

**验证**：
```bash
file ${PROJECT_DIR}/bin/daytona
ldd ${PROJECT_DIR}/bin/daytona
```

### 3.2 构建 computer-use 插件

**输入**：
| 项目 | 说明 |
|------|------|
| 源码目录 | `${PROJECT_DIR}/libs/computer-use/` |
| 入口文件 | `${PROJECT_DIR}/libs/computer-use/main.go` |
| 系统依赖 | libx11-dev, libxtst-dev, gcc |
| 构建脚本 | `${PROJECT_DIR}/hack/computer-use/build-computer-use-amd64.sh` |

**构建命令**：

方式一：Linux amd64 本地构建
```bash
apt-get install -y libx11-dev libxtst-dev gcc
cd ${PROJECT_DIR}/libs/computer-use
go build -o ../dist/libs/computer-use-amd64 main.go
chmod +x ../dist/libs/computer-use-amd64
```

方式二：使用构建脚本（自动选择构建方式）
```bash
mkdir -p ${PROJECT_DIR}/dist/libs
cd ${PROJECT_DIR}
./hack/computer-use/build-computer-use-amd64.sh
```

**输出**：
| 项目 | 说明 |
|------|------|
| 输出文件 | `${PROJECT_DIR}/dist/libs/computer-use-amd64` |
| 文件格式 | ELF 64-bit LSB executable |
| 架构 | Linux amd64 |
| 特性 | 动态链接，依赖 X11 库 |

**验证**：
```bash
file ${PROJECT_DIR}/dist/libs/computer-use-amd64
ldd ${PROJECT_DIR}/dist/libs/computer-use-amd64
```

### 3.3 二进制文件路径映射

| 二进制文件 | 构建输出位置 | Dockerfile 复制目标 | 最终路径 |
|------------|-------------|---------------------|----------|
| daytona | `bin/daytona` | `/usr/local/bin/daytona` | `/usr/local/bin/daytona` |
| computer-use | `dist/libs/computer-use-amd64` | `/usr/local/lib/daytona-computer-use/daytona-computer-use` | `/usr/local/lib/daytona-computer-use/daytona-computer-use` |

---

## 四、完整 Dockerfile

将以下 Dockerfile 放入项目根目录：

```dockerfile
FROM enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest

# 1. 配置 apt 镜像源
RUN echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy main restricted universe multiverse" > /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-updates main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-backports main restricted universe multiverse" >> /etc/apt/sources.list && \
    echo "deb http://mirrors-bak.sk.aipower3.cmbchina.cn/repository/apt/ jammy-security main restricted universe multiverse" >> /etc/apt/sources.list

# 2. 安装基础工具
RUN apt-get update && apt-get install -y \
    curl wget sudo git vim less tree unzip ca-certificates openssl \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

# 3. 安装非 root 用户工具
RUN curl -LsSf https://github.com/containerd/platforms/releases/download/v0.0.0%2Binfinite-2/platforms.tar.gz | tar -xz -C /usr/local/bin/ \
    && curl -LsSf https://github.com/oras-project/oras/releases/download/v1.2.2/oras_1.2.2_linux_amd64.tar.gz | tar -xz -C /usr/local/bin/ \
    && rm -rf oras-install

# 4. 创建用户
RUN useradd -m -s /bin/bash x \
    && echo "x:x" | chpasswd \
    && echo "x ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

# 5. 安装 Node.js
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g \
        tsx@^4.19.2 \
        typescript@^5.0.0 \
        @types/node@^22.0.0 \
        tsc@^2.0.0

# 6. 配置 npm 镜像
RUN npm config set registry http://central.jaf.cmbchina.cn

# 7. 安装 Docker CLI
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.4.1.tgz | tar -xz -C /usr/local/bin/ \
    && chmod +x /usr/local/bin/docker/*

# 8. 环境变量
ENV PYTHONUNBUFFERED=1
ENV DEBIAN_FRONTEND=noninteractive

#######################################
# 9. 复制 daemon 二进制
#######################################
RUN mkdir -p /usr/local/bin
COPY bin/daytona /usr/local/bin/daytona
RUN chmod +x /usr/local/bin/daytona

#######################################
# 10. Volume 挂载点准备
#######################################
RUN mkdir -p /home/x/.daemon/state && \
    mkdir -p /home/x/.daemon/logs && \
    mkdir -p /tmp/daytona-logs && \
    chown -R x:x /home/x/.daemon

#######################################
# 11. 复制 computer-use 插件 (预编译)
#######################################
RUN mkdir -p /usr/local/lib/daytona-computer-use && \
    chown -R x:x /usr/local/lib/daytona-computer-use
COPY --chown=x:x dist/libs/computer-use-amd64 /usr/local/lib/daytona-computer-use/daytona-computer-use
RUN chmod 755 /usr/local/lib/daytona-computer-use/daytona-computer-use && \
    chown x:x /usr/local/lib/daytona-computer-use/daytona-computer-use

#######################################
# 12. 默认环境变量
#######################################
ENV DAYTONA_DAEMON_LOG_FILE_PATH=/tmp/daytona-daemon.log
ENV DAYTONA_ENTRYPOINT_LOG_FILE_PATH=/tmp/daytona-entrypoint.log
ENV DAYTONA_USER_HOME_AS_WORKDIR=true
ENV LOG_LEVEL=info

#######################################
# 13. 端口暴露
#######################################
EXPOSE 8080 2280 22222 22220

#######################################
# 14. 添加 daemon 启动命令到 supervisord
#######################################
RUN printf '\n[program:daytona]\ncommand=/usr/local/bin/daytona --interval 5\nautostart=true\nautorestart=true\nstdout_logfile=/var/log/gem/daytona.log\nstderr_logfile=/var/log/gem/daytona_err.log\nuser=root\n' >> /opt/gem/supervisord.conf

#######################################
# 15. 保持 base image 的 ENTRYPOINT (supervisord)
# 不覆盖，让 supervisord 作为 PID 1 运行
#######################################
```

---

## 五、验证

### 5.1 验证进程

```bash
docker exec test-sandbox bash -c "ps -p 1 && ps auxf | grep daytona"
```

**预期结果**：PID 1 是 supervisord，daytona 进程由 supervisord 管理

### 5.2 验证端口

```bash
docker exec test-sandbox ss -tlnp | grep -E '8080|2280|22222'
```

**预期结果**：
| 端口 | 服务 |
|------|------|
| 8080 | nginx |
| 2280 | daytona API |
| 22222 | daytona SSH |

### 5.3 验证服务

```bash
curl http://localhost:8080       # HTTP 200
curl http://localhost:2280/port # JSON 响应
```

---

## 六、环境变量说明

### 6.1 必须注入的环境变量

由 Daytona Runner 在启动容器时自动注入：

| 环境变量 | 说明 | 示例 |
|----------|------|------|
| `DAYTONA_SANDBOX_ID` | Sandbox 唯一标识符 | `sandbox-abc123` |
| `DAYTONA_SANDBOX_SNAPSHOT` | 基础镜像快照 | `sha256:xxx` |
| `DAYTONA_SANDBOX_USER` | 操作系统用户名 | `x` |
| `DAYTONA_USER_HOME_AS_WORKDIR` | 是否使用用户主目录作为工作目录 | `true` |

### 6.2 可选环境变量

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `DAYTONA_DAEMON_LOG_FILE_PATH` | Daemon 日志文件路径 | `/tmp/daytona-daemon.log` |
| `DAYTONA_ENTRYPOINT_LOG_FILE_PATH` | Entrypoint 日志文件路径 | `/tmp/daytona-entrypoint.log` |
| `ENTRYPOINT_SHUTDOWN_TIMEOUT_SEC` | Entrypoint 关闭超时（秒） | `10` |
| `SIGTERM_SHUTDOWN_TIMEOUT_SEC` | SIGTERM 关闭超时（秒） | `5` |

### 6.3 验证环境变量

```bash
docker exec test-sandbox env | grep DAYTONA
```
