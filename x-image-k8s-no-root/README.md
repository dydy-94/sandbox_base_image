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
│   └── daytona                             # daytona daemon 主 binary（43 MB，含 JWT 鉴权）
├── daemon/                                 # daytona 源码（同事 fork 自 daytonaio，含 JWT 鉴权改造）
│   ├── cmd/                                # 主入口 + config
│   ├── pkg/                                # toolbox / git / ssh / terminal 等
│   ├── go.mod / go.sum                     # 依赖清单
│   ├── Dockerfile / auth.yaml.example     # 同事原版 Dockerfile + 鉴权 yaml 模板
│   └── docs/AUTH.md                        # 鉴权详细说明
├── libs/                                   # workspace 模式的兄弟 module（编译 daemon 用）
│   ├── api-client-go/                      # Go API 客户端
│   ├── common-go/                          # 公共错误/代理（daemon 强依赖）
│   └── computer-use/                       # computer-use 插件源码（编译后产物在 dist/libs/）
├── go.work                                 # Go workspace 配置（让 ./daemon 找到 ./libs/*）
├── dist/
│   └── libs/
│       └── computer-use-amd64              # daytona 电脑使用插件（15 MB，步骤 11 拷进镜像）
├── .dockerignore                           # 限制 build context（建议加）
└── README.md                               # 本文件
```

这个 `Dockerfile` 与项目内 `D:\AIO 新镜像打造\Dockerfile.v2` 是逐字节一致的——只是迁移过来后去掉了 `.v2` 后缀。

> `daemon/`、`libs/`、`go.work` 是为了**重新编译带鉴权的 daemon** 用的，**不会**进入 docker build context（步骤 11 只 COPY `dist/libs/computer-use-amd64`，步骤 9 只 COPY `bin/daytona`）。

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
| 12 | 默认 `ENV` | 5 个环境变量：`DAYTONA_DAEMON_LOG_FILE_PATH`、`DAYTONA_ENTRYPOINT_LOG_FILE_PATH`、`DAYTONA_USER_HOME_AS_WORKDIR`、`LOG_LEVEL`、`DAYTONA_AUTH_ENABLED=false`（**鉴权默认关闭**，运行时通过 K8s ConfigMap/Secret 或 -e 覆盖；详见第 8 节） |
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
- **`bin/daytona` 必须是含 JWT 鉴权的版本**（42~43 MB）。原版 32 MB 的二进制没有鉴权，build 出来的镜像也不会鉴权。详见下方"重编译 daemon"小节。

### 重编译 daemon（如果你改了 `daemon/` 源码或要升级鉴权）

```powershell
cd D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root

# 国内代理 + linux/amd64 交叉编译
$env:GOPROXY='https://goproxy.cn,direct'
$env:GOSUMDB='sum.golang.google.cn'
$env:GOOS='linux'; $env:GOARCH='amd64'; $env:CGO_ENABLED='0'

# ⚠️ 输出文件名必须是 bin\daytona（不能叫 daytona-new-amd64），
# 因为 Dockerfile 步骤 9 写死了 COPY bin/daytona /usr/local/bin/daytona。
# 如果编译到 daytona-new-amd64，build 出来的镜像里 daytona 还是原版，新代码进不去
go build -ldflags "-X 'github.com/daytonaio/daemon/internal.Version=dev'" `
         -o bin\daytona .\daemon\cmd\daemon

Get-Item bin\daytona    # 看 LastWriteTime 是刚才
```

前置依赖（`libs/common-go`、`libs/computer-use`、`libs/api-client-go`、`go.work`）和踩坑细节详见第 8.2 节。

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

### 启用 JWT 鉴权（可选）

镜像默认 `DAYTONA_AUTH_ENABLED=false`，要开启的话 `-e` 加几个环境变量：

```bash
docker run -d \
  --security-opt seccomp=unconfined \
  --shm-size=2g \
  -p 18080:8080 -p 2280:2280 -p 22222:22222 -p 22220:22220 \
  --name aio-test-auth \
  -e DAYTONA_AUTH_ENABLED=true \
  -e DAYTONA_AUTH_VALIDATE_ISSUER=true \
  -e DAYTONA_AUTH_JWT_ISSUER=https://oidc.idc.cmbchina.cn/ \
  aio-daytona-x:v2

# 验证：白名单放行
curl.exe http://localhost:18080/version
# 期望: 200

# 验证：缺 token 拒绝
curl.exe -i -X POST http://localhost:18080/process/execute \
  -H "Content-Type: application/json" -d '{"command":"echo hi"}'
# 期望: 401 {"error":"unauthorized","reason":"missing id-token header"}
```

完整三组测试用例 + K8s ConfigMap/Secret 模板见第 8.3、8.4 节。

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

# 5. 验证 daemon 二进制含 JWT 鉴权（看启动日志）
grep -E 'Auth enabled|JWT auth' /var/log/gem/daytona/daytona.log
# 鉴权关闭时（默认）:
#   JWT auth disabled
# 鉴权开启时（-e DAYTONA_AUTH_ENABLED=true）:
#   Auth enabled: true, algorithm: HS256, header: id-token, ExcludePaths: [...]
#   JWT auth enabled: algorithm=HS256, header=id-token, issuer="https://...", audience=""

# 6. 验证请求日志有 auth_result 字段（鉴权开启时）
tail -n 50 /var/log/gem/daytona/daytona.log | grep auth_result
# 期望: auth_result=passed / missing_token / invalid_issuer 等
```

### 6.1 日志落盘位置（重要）

daemon **写两套日志**，路径完全不同：

| 日志类型 | 路径 | 内容 | 怎么看 |
|---|---|---|---|
| **daemon 内部日志**（logrus） | `/tmp/daytona-daemon.log` | 启动 banner、**`auth_result` 鉴权结果**、业务日志 | `kubectl exec ... -- tail -f /tmp/daytona-daemon.log` |
| **supervisord 抓的 stdout/stderr** | `/var/log/gem/daytona/daytona.log` 和 `daytona_err.log` | 进程启动/退出、panic、未被 logrus 接管的输出 | `kubectl exec ... -- tail -f /var/log/gem/daytona/daytona.log` |

⚠️ **`kubectl logs` 默认看不到鉴权日志**——因为 daemon 用 `os.OpenFile` 把日志写到文件里，不走 stdout。要让 `kubectl logs` 能看到，把 [Dockerfile 步骤 12](Dockerfile) 改成：

```dockerfile
ENV DAYTONA_DAEMON_LOG_FILE_PATH=
```

留空 → daemon 走 stderr → supervisord 抓到 → `kubectl logs` 可见。

```bash
# 改完重新 build + 部署
docker buildx build --platform=linux/amd64 -t aio-daytona-x:v2 -f Dockerfile --load .
kubectl rollout restart deployment/daytona-daemon

# 触发一次鉴权失败
curl -X POST http://<sandbox-svc>:8080/process/execute -H "Content-Type: application/json" -d '{"command":"ls"}'

# kubectl logs 看 auth_result
kubectl logs <sandbox-pod> | grep auth_result
# 期望: {"auth_result":"missing_token","reason":"missing id-token header", ...}
```

⚠️ 这两个路径**都没挂到宿主机或 PVC**，Pod 重建就丢。生产环境如果要审计，**单独挂一个 emptyDir 或 PVC 到 `/tmp`**，或者改成 stdout 后用 K8s 日志收集（Fluentd / Loki / Vector 等）。

---

## 7. 我们没改的（故意保留）

| 项 | 原因 |
|---|---|
| `DISABLE_JUPYTER` / `DISABLE_CODE_SERVER` | 保留关闭——非 root 沙箱里这两个用不到 |
| 不包 `/opt/application/run.sh` 在 wrapper 里 | 让基础镜像升级 run.sh 时我们不丢同步 |
| Python 版本钉到 `3.13` | 与上游一致 |
| 步骤 14 的 `environment=` 那 4 个 ENV | 与上游一致 |

---

## 8. JWT 鉴权改造（同事在 daemon 源码上加的接口鉴权）

### 8.1 改了什么

源码目录 `daemon/` 里加了 3 个鉴权中间件文件，修改了 4 个原有文件：

**新增文件（3 个，均在 `daemon/pkg/toolbox/middlewares/`）：**

| 文件 | 作用 |
|---|---|
| `auth.go` | gin `JWTAuthMiddleware`；处理 disabled / 白名单 / missing / invalid / 失败状态码 |
| `jwt_verifier.go` | `JWTVerifier`；负责签名/exp/nbf/iss/aud 校验；支持 HS256/384/512、RS/PS256/384/512、ES256/384/512 |
| `auth_context.go` | `AuthContext` + 11 个 `AuthResult` 枚举（passed/disabled/skipped/missing_token/invalid_token/invalid_signature/expired/not_yet_valid/invalid_issuer/invalid_audience/misconfigured） |

**修改文件（4 个）：**

| 文件 | 改了什么 |
|---|---|
| `daemon/cmd/daemon/config/config.go` | 新增 `AuthConfig` 和 `AccessLogConfig` 结构；新增 `loadAuthConfigFromFile` 按 `$HOME/.daemon/auth.yaml` → `/home/x/.daemon/auth.yaml` → `/etc/daytona/auth.yaml` 顺序加载；envconfig 优先级高于 yaml |
| `daemon/pkg/toolbox/toolbox.go` | `Server` 结构体加 `AuthConfig` 字段；`Start()` 中条件注册 JWT 中间件 |
| `daemon/cmd/daemon/main.go` | 构造 `toolbox.Server` 时把 `*config.Config` 注入到 `AuthConfig` |
| `daemon/pkg/toolbox/middlewares/logging.go` | 重写访问日志签名 `LoggingMiddleware(cfg *daemonconfig.Config)`；按需记录 headers/body/auth；敏感头（authorization/cookie/x-api-key/x-auth-token）值 base64 编码脱敏 |
| `daemon/go.mod` | 新增 `github.com/golang-jwt/jwt/v5 v5.2.1` |
| `daemon/Dockerfile`（`x-image-k8s-no-root/Dockerfile` 步骤 14） | 新增 `ENV DAYTONA_AUTH_ENABLED=false`（默认关闭） |

详细鉴权流程参考 `daemon/docs/AUTH.md`。

### 8.2 本地编译这个 daemon

#### 为什么不能直接 `go mod tidy`

`daemon/go.mod` 里 import 了 `github.com/daytonaio/common-go`，但没声明这个依赖——原版是 monorepo，靠 `go.work` workspace 解析。我们这边是从 monorepo 拆出来的子目录，**单跑 `go mod tidy` 会报 `cannot find module providing package github.com/daytonaio/common-go/...`**。

#### 操作步骤

```powershell
# 1) 把 common-go、computer-use、api-client-go 这 3 个 Go module 拷贝到 x-image-k8s-no-root/libs/
#    从同事给的完整 daytona_cmb 仓库拷：
Copy-Item -Recurse 'C:\Users\Walege\Desktop\daytona\cmb_daytona\daytona_cmb\libs\common-go' `
          'D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root\libs\common-go'
Copy-Item -Recurse 'C:\Users\Walege\Desktop\daytona\cmb_daytona\daytona_cmb\libs\computer-use' `
          'D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root\libs\computer-use'
Copy-Item -Recurse 'C:\Users\Walege\Desktop\daytona\cmb_daytona\daytona_cmb\libs\api-client-go' `
          'D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root\libs\api-client-go'

# 2) 在 x-image-k8s-no-root/ 下建 go.work（不要再放到 daemon/ 子目录下）
@"
go 1.25.4

use (
    ./daemon
    ./libs/api-client-go
    ./libs/common-go
    ./libs/computer-use
)
"@ | Out-File -Encoding utf8 'D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root\go.work'

# 3) build 命令必须在 x-image-k8s-no-root/ 跑（go.work 所在目录），不能再 cd 到 daemon/
cd D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root

# 4) 设国内代理 + Go 1.25.4 toolchain
$env:GOPROXY='https://goproxy.cn,direct'
$env:GOSUMDB='sum.golang.google.cn'
$env:GOOS='linux'; $env:GOARCH='amd64'; $env:CGO_ENABLED='0'

# 5) 编译（路径前缀必须加 .\daemon\，因为是 workspace 模式）
#    输出文件名必须是 bin\daytona（不能叫 daytona-new-amd64），
#    因为 Dockerfile 步骤 9 写死了 COPY bin/daytona /usr/local/bin/daytona。
#    如果编译到 daytona-new-amd64，build 出来的镜像里 daytona 还是原版，新代码进不去
go build -ldflags "-X 'github.com/daytonaio/daemon/internal.Version=dev'" `
         -o bin\daytona .\daemon\cmd\daemon

# 6) 验证覆盖成功
Get-Item bin\daytona   # 看 LastWriteTime 是刚才
```

#### Go 版本要求

`daemon/go.mod` 第 3 行写的是 `go 1.25.4`，本地 Go 必须 ≥ 1.25.4。如果低于：

```powershell
go version   # 看当前版本
# 升级：去 https://go.dev/dl/ 下载 go1.25.4.windows-amd64.zip
# 或者让 Go 自动下载 toolchain（不要设 GOTOOLCHAIN=local）
```

#### 必须修补的代码 bug

同事给过来的 `daemon/pkg/toolbox/middlewares/logging.go` 第 5 行**漏了 import `encoding/base64`**——文件用了 `base64.StdEncoding.EncodeToString` 但没引包，编译会报：

```
daemon\pkg\toolbox\middlewares\logging.go:141:9: undefined: base64
```

补上：

```go
import (
    "bytes"
    "encoding/base64"   // ← 加这一行
    "encoding/json"
    ...
)
```

### 8.3 打镜像 + 测鉴权

```powershell
cd D:\AIO-GIT\sandbox_base_image\x-image-k8s-no-root

# 打镜像
docker buildx build --platform=linux/amd64 --progress=plain `
  -t aio-daytona-x:v2 -f Dockerfile --load .

# 跑起来（带 iss 校验配置）
docker run -d --security-opt seccomp=unconfined --shm-size=2g `
  -p 18080:8080 -p 2280:2280 -p 22222:22222 -p 22220:22220 `
  --name aio-test `
  -e DAYTONA_AUTH_ENABLED=true `
  -e DAYTONA_AUTH_VALIDATE_ISSUER=true `
  -e DAYTONA_AUTH_JWT_ISSUER=https://oidc.idc.cmbchina.cn/ `
  aio-daytona-x:v2

Start-Sleep -Seconds 30

# 三组测试
# 1) 白名单放行（/version 在默认白名单里）
curl.exe http://localhost:18080/version
# 预期: 200

# 2) 缺 token → 401
curl.exe -i -X POST http://localhost:18080/process/execute `
  -H "Content-Type: application/json" -d '{"command":"echo hi"}'
# 预期: 401 {"error":"unauthorized","reason":"missing id-token header"}

# 3) iss 校验（用 pyjwt 现场签两个 token）
pip install pyjwt
python -c "import jwt,time;t=int(time.time());print(jwt.encode({'iss':'https://oidc.idc.cmbchina.cn/','sub':'test','exp':t+3600},'s',algorithm='HS256'));print(jwt.encode({'iss':'https://evil.com/','sub':'test','exp':t+3600},'s',algorithm='HS256'))"
# 把两个 token 复制出来
$TOKEN_OK='<iss 对的>'
$TOKEN_BAD='<iss 错的>'

curl.exe -i -X POST http://localhost:18080/process/execute `
  -H "Content-Type: application/json" -H "id-token: $TOKEN_OK" `
  -d '{"command":"echo hi"}'
# 预期: 不是 401（鉴权通过；业务层可能报 command not found，但鉴权是 OK 的）

curl.exe -i -X POST http://localhost:18080/process/execute `
  -H "Content-Type: application/json" -H "id-token: $TOKEN_BAD" `
  -d '{"command":"echo hi"}'
# 预期: 401 {"error":"unauthorized","reason":"invalid issuer"}

# 看日志确认
docker exec aio-test tail -f /var/log/gem/daytona/daytona.log | Select-String auth_result
# 应看到: auth_result=passed / missing_token / invalid_issuer
```

### 8.4 鉴权配置方式（运行时）

daemon 启动时按以下顺序加载（**配置不可热加载，改完必须 `rollout restart` 或重建容器**）：

| 优先级 | 来源 | 查找路径 |
|---|---|---|
| 低 | YAML 配置文件 | `$HOME/.daemon/auth.yaml` → `/home/x/.daemon/auth.yaml` → `/etc/daytona/auth.yaml` |
| 高 | 环境变量 | `DAYTONA_AUTH_*` |

K8s 推荐用法（配置和镜像解耦）：

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: daytona-auth
data:
  DAYTONA_AUTH_ENABLED: "true"
  DAYTONA_AUTH_VALIDATE_ISSUER: "true"
  DAYTONA_AUTH_JWT_ISSUER: "https://oidc.idc.cmbchina.cn/"
---
apiVersion: v1
kind: Secret
metadata:
  name: daytona-auth-secret
stringData:
  DAYTONA_AUTH_JWT_SECRET: "your-prod-secret-xxx"
---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: daytona
          image: aio-daytona-x:v2
          envFrom:
            - configMapRef: { name: daytona-auth }
            - secretRef:    { name: daytona-auth-secret }
```

修改 ConfigMap/Secret 后**必须** `kubectl rollout restart deployment/daytona-daemon` 才会生效——daemon 进程不监听 env 变化。

### 8.5 鉴权行为速查

| `DAYTONA_AUTH_VALIDATE_*` | 默认 | 作用 |
|---|---|---|
| `VALIDATE_SIGNATURE` | false | 是否校验 JWT 签名 |
| `VALIDATE_EXPIRATION` | false | 是否校验 `exp`（过期）/ `nbf`（尚未生效） |
| `VALIDATE_ISSUER` | false | 是否校验 `iss`（签发方） |
| `VALIDATE_AUDIENCE` | false | 是否校验 `aud`（接收方） |

⚠️ **安全警告**：`DAYTONA_AUTH_ENABLED=true` 但 4 个 VALIDATE 开关都不开 → daemon 只做 base64 解码 + JSON 解析，**任何人拿任意字符串当 token 都能进**。生产环境**必须**至少开 `VALIDATE_SIGNATURE=true`。

⚠️ **iss 校验有个坑**：配置了 `VALIDATE_ISSUER=true` 但 `jwt_issuer` 留空时，`jwt_verifier.go` 第 207 行的判断是 `if v.cfg.JWTIssuer != "" && iss != v.cfg.JWTIssuer`——**留空就跳过校验**，等于"开了白开"。

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