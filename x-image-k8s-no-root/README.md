# AIO 沙箱镜像 — `x-image-k8s-no-root`

基于 volces 闭源基础镜像
`enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest`
做的二次定制版。相对原版有 **3 处生产环境必需的改动**：

1. **Daytona Daemon 以 `x` 用户（非 root）身份运行**（UID/GID 1000）。
2. **关闭 Jupyter 和 code-server**（`DISABLE_JUPYTER=true`、`DISABLE_CODE_SERVER=true`）。
3. **Daemon 日志写到 `/var/log/gem/daytona/`**，让 supervisord 的 `[program:daytona]` 块能在 K8s sandbox resume 时不崩。

其余部分与上游一致。

---

## 1. 目录结构

```
x-image-k8s-no-root/
├── Dockerfile                              # 唯一的 build 指令
├── bin/
│   └── daytona                             # daytona daemon 主 binary（30 MB）
├── dist/
│   └── libs/
│       └── computer-use-amd64              # daytona 电脑使用插件（100 MB）
├── .dockerignore                           # 限制 build context（建议加）
└── README.md                               # 本文件
```

这个 `Dockerfile` 与项目内 `D:\AIO 新镜像打造\Dockerfile.v2` 是逐字节一致的——只是迁移过来后去掉了 `.v2` 后缀。

---

## 2. Dockerfile 各步骤说明

| # | 步骤 | 作用 |
|---|---|---|
| 1 | `ENV` 头 | 关闭 Jupyter/code-server、设 `USER=x UID=1000 GID=1000`、把 `PATH` 指向 x 的 npm-global、配 Anthropic / Python / Go 镜像和 `BROWSER_EXTRA_ARGS` |
| 2 | `apt-get install` | curl/wget/git/vim + fcitx5 中文输入法 + deadsnakes 源装 `python3.13` + pip + supervisor + fastmcp + cffi + cryptography + oras |
| 3 | NodeSource | 装 Node.js 22 系统级、设 npm 镜像源 |
| 4 | `useradd x` + mkdir | 建 x 用户和 home 子目录（npm / cache / claude / claude-code-router / mcp2rest）|
| 4b | 防御性 mkdir | 提前建 `/home/x/.config/browser/`——防止挂上来的持久卷里有 root 权限残留文件 |
| 5 | npm global install | `tsx`、`typescript`、`mcp2rest`、`@anthropic-ai/claude-code@2.1.63`、`@anthropic-ai/claude-agent-sdk@0.2.63`、`@musistudio/claude-code-router`、`pm2` |
| 6 | 私有源 | npm + pip 指向公司 Artifactory 镜像 |
| 7 | apt 源 | 把 `/etc/apt/sources.list` 改成公司 jammy 私仓镜像 |
| 8 | mcp2rest 配置 | 写 `/home/x/.mcp2rest/config.yaml`，含 chrome-devtools-mcp 条目 |
| 9 | `COPY bin/daytona` | 把 daytona daemon 主 binary 放到 `/usr/local/bin/`，chown 给 x |
| 10 | volume 目录 | 建 `/home/x/.daemon/{state,logs}`、`/tmp/daytona-logs`、**`/var/log/gem/daytona/`**——最后这个是步骤 14 的关键 |
| 11 | `COPY dist/libs/computer-use-amd64` | 把 computer-use 插件放到 `/usr/local/lib/daytona-computer-use/daytona-computer-use`，chown 给 x |
| 12 | 默认 `ENV` | 4 个 daytona 需要的 `DAYTONA_*` / `LOG_LEVEL` 环境变量 |
| 13 | `EXPOSE` | 8080（nginx）、2280、22222、22220（daytona SSH）|
| 14 | `[program:daytona]` | append supervisord block：以 `user=x` 跑 `/usr/local/bin/daytona --interval 5`，stdout/stderr 写到 `/var/log/gem/daytona/` |
| 15 | ENTRYPOINT | 不显式设置——保留基础镜像的 `/opt/application/run.sh` |

---

## 3. 为什么日志写到 `/var/log/gem/daytona/`（非 root 的关键设计）

### 3.1 supervisord 的硬性要求

`supervisord` 启动 program 时**不会自动创建 `stdout_logfile` 的父目录**——父目录不存在直接启动失败，整个容器立刻退出（exit 255）。

所以步骤 14 的 stdout/stderr 路径必须指向**已经在镜像里存在的、可写的**父目录。

### 3.2 为什么 no-root 版必须新建子目录

daemon 在 no-root 版以 `user=x`（UID 1000）身份运行，**没有 root 特权**。

| 候选路径 | 父目录默认 owner | x 用户可写吗？ |
|---|---|---|
| `/var/log/gem/` | root:root 755 | ❌ 不能 |
| `/tmp/` | root:root 1777 | ✅ 但 tmpfs 重启清空 |
| `/home/x/.daemon/` | root:root 755 | ❌ 不能 |
| `/opt/gem/` | root:root 755 | ❌ 不能 |

**最简单的解法**：在基础镜像自带的 `/var/log/gem/` 下新建一个 `daytona/` 子目录，把它 chown 给 x。这样：

- 父目录 `/var/log/gem/` 是基础镜像层，K8s sandbox resume 不影响
- 子目录 `/var/log/gem/daytona/` 是 x 拥有，x 用户能写
- supervisord 启动时父目录已存在，程序顺利拉起

### 3.3 步骤 14 的最终写法

```dockerfile
RUN printf '\n[program:daytona]\ncommand=/usr/local/bin/daytona --interval 5\nautostart=true\nautorestart=true\nstdout_logfile=/var/log/gem/daytona/daytona.log\nstderr_logfile=/var/log/gem/daytona/daytona_err.log\nuser=x\nenvironment=...\n' >> /opt/gem/supervisord.conf
```

配合步骤 10 的 mkdir + chown：

```dockerfile
RUN mkdir -p /var/log/gem/daytona && \
    chown -R x:x /var/log/gem/daytona
```

### 3.4 为什么不用其他方案

| 方案 | 否决原因 |
|---|---|
| 改写 `/tmp/` | tmpfs 重启清空，sandbox resume 时容器重启会让 `/tmp` 重置——日志丢失 |
| chmod 777 `/var/log/gem/` | 污染基础镜像配置，其他 volces 服务可能也用这个目录 |
| chown x:x `/var/log/gem/` | 同上，影响其他服务 |
| 在 `/opt/gem/` 下新建子目录 | `/opt/gem/` 是 volces 编排层专用，跟日志无关 |

**选新建 `/var/log/gem/daytona/` 子目录是因为**：对基础镜像侵入最小，仅影响新增的 1 个目录。

---

## 4. 怎么 build

### 前置条件
- Docker 24+（带 buildx）
- 网络能拉 `enterprise-public-cn-beijing.cr.volces.com`（基础镜像）
- 网络能拉 `central.jaf.cmbchina.cn`（公司 npm/pip 镜像，运行时）
- 目录里有 `bin/daytona` 和 `dist/libs/computer-use-amd64` 两个 binary

### 一次性 build

```bash
cd x-image-k8s-no-root

# (可选) 清旧 buildx 缓存，避免误用旧 layer
docker buildx prune -f --filter "until=24h"

# build + load 到本地 daemon
docker buildx build \
  --platform=linux/amd64 \
  --progress=plain \
  -t aio-daytona-x:v2 \
  -f Dockerfile \
  --load \
  .
```

### 带 retry 的 build（VPN 不稳定时推荐）

```bash
for i in 1 2 3 4 5; do
  echo "=== attempt $i/5 ==="
  docker buildx build \
    --platform=linux/amd64 \
    --progress=plain \
    -t aio-daytona-x:v2 \
    -f Dockerfile \
    --load \
    . && break
  echo "attempt $i failed, sleeping 30s before retry"
  sleep 30
done
```

5 次都失败的话最常见原因：
- `ghcr.io` 被 VPN 屏蔽——切 VPN 节点，或在 build context 里直接带 binary（**我们这里就是这么做的**）
- `pip install pypi.org` 超时——Dockerfile 里设 `PIP_INDEX_URL` 走镜像
- `golang go install` 失败——Dockerfile 里设 `GOPROXY=https://goproxy.cn,direct`

### Save 镜像

```bash
# 纯 tar（5-10 GB）
docker save -o aio-daytona-x-v2.tar aio-daytona-x:v2

# 压缩版（推荐）
docker save aio-daytona-x:v2 | gzip > aio-daytona-x-v2.tar.gz
```

### 推送镜像

```bash
docker tag aio-daytona-x:v2 <registry-host>/<namespace>/aio-daytona-x:v2
docker push <registry-host>/<namespace>/aio-daytona-x:v2
```

---

## 5. 怎么跑 / 验证

### 本地冒烟测试（不需要 K8s）

```bash
docker run -d \
  --security-opt seccomp=unconfined \
  --shm-size=2g \
  -p 18080:8080 -p 2280:2280 -p 22222:22222 -p 22220:22220 \
  --name aio-test \
  aio-daytona-x:v2

# 等 supervisord 把所有 service 拉起来
sleep 30

# 验证 daytona 是 x 用户跑（不是 root）
docker exec aio-test ps -eo user,pid,cmd | grep daytona
# 期望:
#   x   92  ... /usr/local/bin/daytona --interval 5

# 验证 daemon 日志文件被创建（这就是我们 fix 的回归）
docker exec aio-test ls -la /var/log/gem/daytona/
# 期望:
#   drwxr-xr-x ... x x ... daytona/
#   -rw-r--r-- ... x x ... daytona/daytona.log
#   -rw-r--r-- ... x x ... daytona/daytona_err.log

# 浏览器打开 AIO dashboard
#   http://localhost:18080
```

### 推到 K8s
- 把镜像 tag 填到 sandbox template 的 `image:` 字段
- sandbox template 的 volumeMounts 必须有：`/home/x/experts`、`/home/x/projects`、`/home/x/.data`、`/home/x/.x/xmemory`——其他 `/home/x/` 路径会在每次 `Resume` 时被清
- **不要** mount `/var/log/`——daemon 日志要留在镜像层里

---

## 6. 验证清单

进容器后跑：

```bash
# 1. daytona 是 x 用户跑
ps -eo user,pid,cmd | grep daytona
#   x   92  ... /usr/local/bin/daytona --interval 5

# 2. supervisord 加载了 daytona program
supervisorctl status daytona
#   daytona   RUNNING   pid 92, uptime 0:01:23

# 3. ENV 透传成功
cat /proc/92/environ | tr '\0' '\n' | grep -E 'DAYTONA|LOG_LEVEL'
#   DAYTONA_DAEMON_LOG_FILE_PATH=/tmp/daytona-daemon.log
#   DAYTONA_ENTRYPOINT_LOG_FILE_PATH=/tmp/daytona-entrypoint.log
#   DAYTONA_USER_HOME_AS_WORKDIR=true
#   LOG_LEVEL=info

# 4. 日志目录 x 可写
ls -ld /var/log/gem/daytona/
#   drwxr-xr-x 2 x x  ... /var/log/gem/daytona/
```

---

## 7. 我们没改的（故意保留）

| 项 | 原因 |
|---|---|
| `DISABLE_JUPYTER` / `DISABLE_CODE_SERVER` | 保留关闭——非 root 沙箱里这两个用不到 |
| 不包 `/opt/application/run.sh` 在 wrapper 里 | 让基础镜像升级 run.sh 时我们不丢同步 |
| Python 版本钉到 `3.13` | 与上游一致 |
| 步骤 14 的 `environment=` 那 4 个 ENV | 与上游一致 |

---

## 8. 已知局限

- `computer-use` 插件可能会报 `permission denied`——这是 daytona 源码内部的 binary 路径配置问题（跟 Dockerfile 无关）。daemon 看到后会 graceful 降级：`Continuing without computer-use functionality...`，主服务（API/SSH/Pty）不受影响。
- `BROWSER_EXTRA_ARGS` 里写了公司内网的 Anthropic endpoint 和占位 token——**正式部署前**必须替换成生产值。

---

## 9. 跟 `root-daemon-version/Dockerfile` 的对比

本目录的 `Dockerfile`（非 root daemon 版）vs 同目录下的 `root-daemon-version/Dockerfile`（root daemon 原始版）的所有差异。

### 9.1 改动汇总

| # | 改动点 | root 版 | no-root 版（本目录）| 改动必要性 |
|---|---|---|---|---|
| 1 | 新增 ENV 头 | （无 USER=x 配置）| `USER=x`、`USER_UID=1000`、`USER_GID=1000`、`HOME=/home/x`、`NPM_CONFIG_PREFIX` 等 6 行 | **核心**——让 aio 编排层知道以 x 身份跑 |
| 2 | 新增 ENV `DISABLE_JUPYTER` / `DISABLE_CODE_SERVER` | 没设（默认启用）| `=true` 关掉 | 避免非 root 沙箱里出现不需要的组件 |
| 3 | 步骤 9 daytona 二进制 | `chmod +x` | `chmod +x && chown x:x` | **核心**——让 x 能执行 daemon |
| 4 | 步骤 10 daytona 日志目录 | `mkdir -p /home/x/.daemon/state`、`/home/x/.daemon/logs` | 多了 `mkdir -p /var/log/gem/daytona` | **核心**——x 用户能写的日志位置 |
| 5 | 步骤 10 chown | `chown -R x:x /home/x/.daemon` | 同 + `chown -R x:x /var/log/gem/daytona` | 配套 4 |
| 6 | 步骤 14 `[program:daytona]` user | `user=root` | `user=x` | **核心**——这是 non-root 的关键 |
| 7 | 步骤 14 stdout_logfile | `/var/log/gem/daytona.log` | `/var/log/gem/daytona/daytona.log` | x 用户无写 `/var/log/gem/`，必须放子目录 |
| 8 | 步骤 14 stderr_logfile | `/var/log/gem/daytona_err.log` | `/var/log/gem/daytona/daytona_err.log` | 同上 |
| 9 | 步骤 14 `environment=` 行 | （没有）| 4 个 `DAYTONA_*` + `LOG_LEVEL=info` | 显式传 env 给 daytona process |
| 10 | 步骤 12 默认 ENV | （没有）| 4 个 `DAYTONA_*` + `LOG_LEVEL=info` | 给基础镜像层注入 env |
| 11 | 新增步骤 4b 防御性 mkdir | （没有）| `mkdir -p /home/x/.config/browser && chown -R x:x` | 防止持久卷 root 权限残留文件导致启动失败 |

### 9.2 步骤 14 的具体对比

root 版（原始）：
```dockerfile
RUN printf '\n[program:daytona]\ncommand=/usr/local/bin/daytona --interval 5\nautostart=true\nautorestart=true\nstdout_logfile=/var/log/gem/daytona.log\nstderr_logfile=/var/log/gem/daytona_err.log\nuser=root\n' >> /opt/gem/supervisord.conf
```

no-root 版（本目录）：
```dockerfile
RUN printf '\n[program:daytona]\ncommand=/usr/local/bin/daytona --interval 5\nautostart=true\nautorestart=true\nstdout_logfile=/var/log/gem/daytona/daytona.log\nstderr_logfile=/var/log/gem/daytona/daytona_err.log\nuser=x\nenvironment=DAYTONA_DAEMON_LOG_FILE_PATH="/tmp/daytona-daemon.log",DAYTONA_ENTRYPOINT_LOG_FILE_PATH="/tmp/daytona-entrypoint.log",DAYTONA_USER_HOME_AS_WORKDIR="true",LOG_LEVEL="info"\n' >> /opt/gem/supervisord.conf
```

**diff 解读**：
| 行 | 改动 |
|---|---|
| stdout_logfile | `/var/log/gem/daytona.log` → `/var/log/gem/daytona/daytona.log`（**多一层子目录**：x 用户写不了父目录 `/var/log/gem/`）|
| stderr_logfile | 同上 |
| user | `root` → `x`（**这是核心改动**）|
| environment= | 新增 4 个 env 透传 |

### 9.3 为什么用 `/var/log/gem/daytona/` 而不是 `/home/x/.daemon/logs/`

**原始 Dockerfile 用的什么**：

```dockerfile
# 原始 Dockerfile 步骤 14（L159-160）：
stdout_logfile=/var/log/gem/daytona.log
stderr_logfile=/var/log/gem/daytona_err.log
```

注意：**原始版本是直接写到 `/var/log/gem/`**（volces 基础镜像自带这个目录），没建子目录。但那是 `user=root` 跑 daytona——root 可以写任何路径。

**no-root 版为什么不能照搬**：

- daytona 在 no-root 版是 `user=x` 跑（UID 1000）
- `/var/log/gem/` 默认 `root:root` 拥有，模式 `755`
- x 用户只能 `r-x`，**写不了**

**所以 no-root 版必须二选一**：

| 选项 | 做法 | 缺点 |
|---|---|---|
| A. chmod/chown 整个 `/var/log/gem/` | `chmod 777` 或 `chown x:x` | **污染**基础镜像配置；其他 volces 服务可能也用这个目录 |
| B. 在 `/var/log/gem/` 下新建子目录 `daytona/` 并 chown 给 x | `mkdir -p /var/log/gem/daytona && chown x:x` | **侵入最小**——只动新增的子目录 |

我们选了 **B**——这就是 `/var/log/gem/daytona/daytona.log` 多了一层 `daytona/` 的原因。

**supervisord 启动 program 时不会自动创建 stdout_logfile 的父目录**——父目录不存在就启动失败。所以步骤 10 必须先 mkdir + chown 这个新子目录（参考 9.5）。

### 9.4 步骤 9 daytona 二进制的对比

root 版：
```dockerfile
COPY bin/daytona /usr/local/bin/daytona
RUN chmod +x /usr/local/bin/daytona
```

no-root 版：
```dockerfile
COPY bin/daytona /usr/local/bin/daytona
RUN chmod +x /usr/local/bin/daytona && \
    chown x:x /usr/local/bin/daytona
```

只多了一个 `chown x:x`——必须让 x 用户能 exec 这个 binary。

### 9.5 步骤 10 volume 目录的对比

root 版：
```dockerfile
RUN mkdir -p /home/x/.daemon/state && \
    mkdir -p /home/x/.daemon/logs && \
    chown -R x:x /home/x/.daemon
```

no-root 版：
```dockerfile
RUN mkdir -p /home/x/.daemon/state && \
    mkdir -p /home/x/.daemon/logs && \
    mkdir -p /tmp/daytona-logs && \
    mkdir -p /var/log/gem/daytona && \
    chown -R x:x /home/x/.daemon && \
    chown x:x /tmp/daytona-logs && \
    chown -R x:x /var/log/gem/daytona
```

新增了 3 个目录 + 3 个 chown——为了让非 root daemon 能写日志。

### 9.6 文件名 / 位置

| 项 | root 版 | no-root 版 |
|---|---|---|
| 文件名 | `Dockerfile` | `Dockerfile`（本目录）|
| 子目录 | `root-daemon-version/` | 当前目录 |
| 用途 | 旧版（v1 / 内部镜像）| 当前生产版本 |

### 9.7 总结改动大小

**总共 11 处改动**——围绕 3 个核心目标：

1. **daemon 以 x 用户跑**（user=root → user=x + chown 让 x 能 exec + chown 日志目录）
2. **daemon 日志写到基础镜像层**（避免 sandbox resume 清掉）
3. **防御性 mkdir**（防止持久卷 root 权限残留文件）

**核心改动 = 第 6 行（user=root → user=x）**——这一行没改，整个 no-root 改造就失败。