# AIO Sandbox — final-version

> 合并版 AIO Sandbox Docker 镜像,从开源 `aio-sandbox-2.0.1`
> (reverse-engineered) 一路改造到 v13-fix9.x 系列生产可用版本,
> 全部打包在一个 `Dockerfile.final` + 离线资源目录里。
> **build context 根 = `aio-sandbox/` 目录本身。**

---

## 目录

1. [起源与改造历程](#1-起源与改造历程)
2. [最终目录结构](#2-最终目录结构)
3. [完整流程(克隆 → 准备 → build → save → run)](#3-完整流程)
4. [Dockerfile 离线 / 在线资源全表](#4-dockerfile-离线--在线资源全表)
5. [镜像内部架构](#5-镜像内部架构)
6. [权限 / no-root 改造说明](#6-权限--no-root-改造说明)
7. [常见问题](#7-常见问题)
8. [离线 / 在线分析](#8-离线--在线分析)

---

## 1. 起源与改造历程

| 阶段 | 内容 |
|---|---|
| 起点 | 开源 `aio-sandbox-2.0.1`(`ghcr.io/agent-infra/sandbox` 镜像的逆向重建版),原 Dockerfile 永远不会公开。**原版直接跑不起来** — 缺 shebang、缺 ENV、缺 ENV 模板、缺 `gettext-base`、缺 `ncurses-term`、缺 `BROWSER_EXECUTABLE_PATH`、缺 noVNC `index.html`、缺 Python venv 依赖,等等 30+ 个独立 bug。 |
| v9 (Dockerfile) | 第一个 `Dockerfile`,完全在线拉镜像,`pip install` 在 build 期间跑,严重依赖 `pypi.org` 和 `dl.google.com`。国内构建常常 502/timeout。 |
| v9 → v10 | 引入"离线优先"思路:host 端预下载所有 .deb / wheels / npm tgz,Dockerfile 用 `COPY + --no-index --find-links` 离线安装。 |
| v10-final | 修完 11 个独立 bug,Dockerfile 收敛到"冷启动即用"。 |
| v11 (no-code / no-bun) | 移除 code-server 和 bun,精简镜像。 |
| v12 (image slim) | 体积分析,~7.4GB,做 P0-P4 瘦身。 |
| v13-fix9.1 ~ 9.4 | no-root 改造:supervisord 子进程全部以 user=x 运行;`mcp2rest` 路径修复。 |
| v13-fix9.7 | npm-global PATH shim(让 `npm exec` 找到预装的 `chrome-devtools-mcp`)。 |
| v13-fix9.10 | 双向剪贴板 / dbus 路径修复。 |
| v13-fix9.11 | **black-screen fix**:fcitx5 + dbus-session 启动顺序(`priority=815/820`),chrome `priority=920` 在 openbox 之后;`fcitx5` Ctrl+Shift 触发键;`/etc/environment` 注入 `ENABLE_CJK_IME/DISABLE_JUPYTER` 等;`DISABLE_JUPYTER=true` 跳过 jupyter router。 |
| **v13-fix9.11-final (本仓库)** | **合并版**:把 `Dockerfile.offline` + `Dockerline.offline.patch` + patch 系列所有关键修复,合成一个独立 `Dockerfile.final`。不再有 base image + patch 两阶段,直接 build 出最终镜像。 |

**关键决策**:patch 阶段需要 `BASE_IMAGE=aio-sandbox:v13-fix9` (需要先 build base 才能 patch),而 final-version 一次 build 出最终镜像,省去两阶段编排。

---

## 2. 最终目录结构

```
open-source-final-version/
└── aio-sandbox/                    ← build context 根
    ├── Dockerfile.final            ← 唯一入口的 Dockerfile (offline + patch 合并)
    ├── .dockerignore               ← 排除 build context 中的大文件
    ├── cli/.gitkeep
    ├── guard/                      ← sandbox_guard + sandbox_reconcile (扁平化,no-root)
    ├── supervisord/                ← 3 个 Guard conf (备份,主用 docker/supervisord/)
    └── docker/
        ├── README.md                ← 旧版 README (保留作历史参考)
        ├── .dockerignore
        ├── requirements/base-3.13.txt
        ├── supervisord/            ← 4 个 Guard conf (被 Dockerfile 引用)
        └── context/
            ├── prepare-all.sh       ← ⭐ 统一离线下载入口
            ├── prepare-apt-archives.sh ← 769+ apt .deb
            ├── prepare-wheels.sh    ← python-server pip wheels
            ├── prepare-rust.sh      ← rustup-init + agent-browser
            ├── prepare-npm.sh       ← aio/static-assets npm tgz
            ├── prepare-daytona.sh   ← daytona + computer-use (新建)
            ├── prepare-npm.ps1      ← Windows 版 npm 预下载
            ├── prepare-rust.ps1     ← Windows 版 rust 预下载
            ├── download-missing-debs.ps1
            ├── prepare-extras.py    ← Python 独立版 (无 docker 依赖)
            ├── apt-archives/        ← 770 .deb 文件 (~811 MB)
            ├── wheels/              ← python wheels (~1.5 GB)
            ├── venv-bootstrap/       ← 可选:已构建好的 venv tarball
            ├── npm-tgz/             ← aio/ + static-assets/ + bun/
            ├── aio/                 ← aio CLI npm 源码
            ├── static-assets/       ← swagger-ui/xterm/clipboard
            ├── browser-sdk/        ← python browser_sdk 源码
            ├── python-server/       ← python app + vendors 源码
            ├── chrome-deb/          ← google-chrome-stable_amd64.deb
            ├── novnc/               ← noVNC/ 源码
            ├── websocat/            ← websocat 二进制
            ├── cargo-vendored/      ← agent-browser rust 源码
            ├── rustup-pre/          ← rustup-init 二进制
            ├── repl-servers/        ← nodejs REPL server 源码
            ├── bin/                 ← daytona 二进制
            ├── dist/libs/           ← daytona-computer-use 插件
            ├── code-server/         ← 已废弃 (Dockerfile 不用,见 prepare-apt-archives.sh 备注)
            ├── rootfs/              ← /opt/gem, /opt/application, /etc, /usr/local/bin
            ├── guard/               ← (docker/guard 旧副本,不再引用)
            └── scripts/             ← preflight-wheels.sh
```

> **删除项**:`evaluation/`、`examples/`、`sdk/`、`website/`、`.github/workflows/`、18 份历史 README 文档、`Dockerfile.minimal/original/patch/offline/offline.patch`、调试脚本 — 都已清理。

---

## 3. 完整流程

### 3.1 克隆

```bash
# 推荐:只 clone 一次,把 docker/context/ 当 LFS-tracked directory
# 如果 context/ 太大,可用 git-lfs 或者把 context/ 单独打包上传
git clone https://github.com/<your-org>/aio-sandbox-final.git
cd aio-sandbox-final/aio-sandbox
```

> **关于 GitHub 体积限制**:GitHub 单文件硬上限 100 MB,推荐 50 MB 以下。
> `docker/context/wheels/` 1.5 GB 不适合直接 push git。
> 三种解决方案(任选其一):
> 1. **Git LFS**:`git lfs track "docker/context/wheels/*.whl" && git lfs track "docker/context/apt-archives/*.deb"`
> 2. **外链下载**:把 `wheels/` 和 `apt-archives/` 单独上传到对象存储(S3 / OSS),在 README 给下载链接
> 3. **下载脚本**:**本仓库采用这个方案** — 仓库里只放 `prepare-*.sh` 脚本(几 KB),用户在构建前自行跑 `prepare-all.sh` 下载。仓库本身只有几 MB。

### 3.2 准备离线资源

```bash
# 在能联网的机器上(可与 build 机器不同)执行:
bash docker/context/prepare-all.sh
```

这个脚本会按依赖顺序依次运行:
1. `prepare-apt-archives.sh` — 启动临时 `ubuntu:26.04` 容器,`apt-get install --print-uris` 拿到 URL 列表,curl 拉到 `apt-archives/`,**同时**下载 chrome / noVNC / websocat / code-server tarball。
2. `prepare-wheels.sh` — `pip download` 把 5 tier 依赖拉到 `wheels/`(~1.5 GB)。
3. `prepare-rust.sh` — 下载 `rustup-init` 二进制 + 克隆 `agent-browser` git 仓库。
4. `prepare-npm.sh` — `npm pack` 把 aio / static-assets 依赖打成 `.tgz`。
5. `prepare-daytona.sh` — 下载 daytona daemon + computer-use 插件(新增,见脚本注释)。

跳过某个步骤:`SKIP_NPM=1 SKIP_RUST=1 bash prepare-all.sh`

跳过个别子包:`SKIP_DAYTONA=1 bash prepare-all.sh`

所有子脚本都支持内部 CN mirror,失败时回退到 upstream。

### 3.3 Build

```bash
# 准备好的资源 + 源码都在 build context 根 aio-sandbox/
cd aio-sandbox

# 标准 build
docker buildx build -f Dockerfile.final -t aio-sandbox:final-test --load .

# 完整 build 日志(便于调试)
docker buildx build -f Dockerfile.final -t aio-sandbox:final-test \
    --progress=plain --load . 2>&1 | tee build.log

# 离线 build(假设所有 §8 表中"在线兜底"的包都已经在镜像的 CN mirror 可达,
#            否则需要把 prepare 跑过的 assets 全部 bake)
docker buildx build -f Dockerfile.final -t aio-sandbox:final-test \
    --network=none --load .   # 不推荐,容易卡 apt fallback
```

预期:
- 首次 build 约 **20-30 分钟**(主要时间在 squash 前面的 92 个 layer)。
- 输出:`aio-sandbox:final-test` 镜像约 **4.0-4.2 GB**。

### 3.4 验证 build

```bash
# 1. 启动容器
docker run -d --name aio-final -p 18001:8080 --shm-size=1g aio-sandbox:final-test

# 2. 等待冷启动(~45 秒)
sleep 45

# 3. 健康检查
docker exec aio-final curl -m 10 -o /dev/null -w "ping=%{http_code}\n"        http://127.0.0.1:8080/v1/ping
docker exec aio-final curl -m 10 -o /dev/null -w "dashboard=%{http_code}\n"   http://127.0.0.1:8080/
docker exec aio-final curl -m 10 -o /dev/null -w "vnc=%{http_code}\n"         http://127.0.0.1:8080/vnc/index.html
docker exec aio-final supervisorctl status

# 期望输出:ping/dashboard/vnc=200,所有 supervisord 进程 RUNNING
# (reclaim-plugins EXITED 是正常的 — 它是一次性 reclaim)

# 4. 清理
docker stop aio-final && docker rm aio-final
```

### 3.5 Save / Load 镜像(传输用)

```bash
# 打包
docker save aio-sandbox:final-test | gzip > aio-sandbox-final-test.tar.gz
# 体积约 2-2.5 GB(压缩后)

# 加载(目标机器)
docker load < aio-sandbox-final-test.tar.gz
docker run -d --name aio -p 18001:8080 --shm-size=1g aio-sandbox:final-test
```

### 3.6 推送到私有 registry(可选)

```bash
# 推送到招行 jaf (生产环境)
docker tag aio-sandbox:final-test central.jaf.cmbchina.cn/artifactory/api/docker/aio-sandbox:final-test
docker push central.jaf.cmbchina.cn/artifactory/api/docker/aio-sandbox:final-test
```

> **重要**:不要把 `ANTHROPIC_AUTH_TOKEN` 硬编码到镜像里(目前是 placeholder `your-secret-key`)。生产部署通过 `docker run -e ANTHROPIC_AUTH_TOKEN=...` 注入,或者用 docker secret / k8s secret 挂载。

---

## 4. Dockerfile 离线 / 在线资源全表

详细分类见 [§8](#8-离线--在线分析)。简要:

| 资源 | 来源 | 大小 | 准备脚本 |
|---|---|---|---|
| `apt-archives/*.deb` (770 个) | CN apt mirror (tuna/aliyun/...) | ~811 MB | `prepare-apt-archives.sh` |
| `chrome-deb/*.deb` | dl.google.com | ~130 MB | `prepare-apt-archives.sh` §1 |
| `novnc/noVNC/` | github.com + npmmirror | ~3 MB | `prepare-apt-archives.sh` §3 |
| `websocat/*.musl` | github.com/vi/websocat | ~3 MB | `prepare-apt-archives.sh` §4 |
| `wheels/*.whl` (47+ 个) | pypi.tuna.tsinghua + pypi.org | ~1.5 GB | `prepare-wheels.sh` |
| `rustup-pre/rustup-init` | static.rust-lang.org | ~10 MB | `prepare-rust.sh` §1 |
| `cargo-vendored/agent-browser/` | github.com/vercel-labs | ~5 MB | `prepare-rust.sh` §2 |
| `npm-tgz/aio/*.tgz` | registry.npmmirror.com | ~10 MB | `prepare-npm.sh` |
| `npm-tgz/static-assets/*.tgz` | registry.npmmirror.com | ~200 MB | `prepare-npm.sh` |
| `bin/daytona` | github.com/daytonaio | ~10 MB | `prepare-daytona.sh` (新增) |
| `dist/libs/computer-use-amd64` | github.com/daytonaio | ~5 MB | `prepare-daytona.sh` |
| `requirements/*.txt` | (随源码) | <10 KB | 无 |
| `repl-servers/nodejs/` | (随源码) | <1 MB | 无 |
| `browser-sdk/`, `python-server/` | (随源码) | ~1 MB | 无 |
| `rootfs/`, `guard/`, `supervisord/` | (随源码) | <1 MB | 无 |
| **小计** | | **~2.7 GB** | |

**Build 期间在线拉取**(没办法离线化的):
- `oras` 二进制 (10 MB, GitHub releases)
- `fnm` 二进制 (6 MB, GitHub releases)
- node 22 二进制 (50 MB, npmmirror)  — node 24 已注释掉
- 各种 pip 兜底 (`requests` / `fastmcp`)
- 各种 npm 全局包 (`@anthropic-ai/claude-code` / `playwright` 等,~500 MB)

---

## 5. 镜像内部架构

### 5.1 文件系统布局

```
/opt/
├── aio/                        ← aio CLI 编译产物 (Stage 2)
│   ├── aio.js                  ← esbuild bundle
│   └── index.html              ← dashboard 主入口
├── application/
│   ├── run.sh                  ← 入口脚本 (init-once.sh → run.sh → supervisord)
│   ├── init-once.sh            ← 一次性初始化 (envsubst nginx config)
│   ├── build-fix-so.sh         ← build-time 修复 .so 0 字节
│   ├── sweep-so.py             ← Windows overlayfs Errno 22 workaround
│   ├── post-inst.sh            ← pip install 后处理
│   ├── venv-patch.sh           ← venv 修补
│   ├── patch-browser-cdp.py    ← _rewrite_websocket_urls 修复
│   └── supervisord-wirifier.py
├── gem/                        ← supervisord 编排层
│   ├── supervisord.conf        ← 主配置 (include /opt/gem/supervisord/*.conf)
│   ├── supervisord/            ← 子进程 confs
│   │   ├── browser.conf        ← priority=920 (chrome)
│   │   ├── fcitx5.conf         ← priority=820
│   │   ├── dbus-session.conf   ← priority=815
│   │   ├── nginx.conf          ← priority=900
│   │   ├── openbox.conf
│   │   ├── tigervnc.conf       ← priority=900
│   │   ├── websocat.conf
│   │   ├── mcp-server-browser.conf
│   │   ├── python-server
│   │   ├── daytona.conf        ← user=x (no-root)
│   │   ├── reclaim-plugins.conf
│   │   ├── sandbox-daemon.conf ← 不自动 start (无 config.json)
│   │   ├── sandbox-pm2-runtime.conf
│   │   ├── sandbox-guard-launcher.conf
│   │   ├── visible-chrome.conf ← autostart=false (defensive)
│   │   └── log_tail.conf
│   ├── nginx/                  ← 9 个 nginx conf 模板
│   │   ├── python_srv.conf     ← /v1/* → 127.0.0.1:9988
│   │   ├── ui_browser.conf     ← /vnc/* → 127.0.0.1:9222 (CDP)
│   │   ├── vnc.conf            ← /websockify → 127.0.0.1:5700
│   │   ├── jupyter_lab.conf    ← DISABLE_JUPYTER=true 时不渲染
│   │   ├── legacy.conf         ← /healthz, /v1/ping 等
│   │   ├── header_proxy.conf
│   │   ├── mcp_hub.conf
│   │   ├── opencode.conf
│   │   └── gembrowser_compat.conf
│   ├── run.sh                  ← supervisord 包装 (兼容闭源)
│   ├── supervisord.conf        ← 主配置
│   ├── nodejs.sh               ← nodejs env shim
│   ├── openbox.sh / openbox.xml
│   ├── start-browser.sh        ← Chrome 启动器
│   ├── wait-for-python-server.sh
│   ├── fcitx5/profile + config ← DefaultIM=pinyin + Ctrl+Shift 触发
│   ├── skills/                 ← aio-sandbox + aio-local + shared
│   └── vscode/                 ← code-server user settings (no-code build 不使用)
├── browser-ui/index.html       ← Stage 3 产物
├── novnc/noVNC/                ← Stage 9 产物
├── nodejs/22/                  ← fnm install 22 产物
├── python3.13/bin/python       ← /usr/bin/python3 symlink
└── server-venv/                ← python-server 隔离 venv
    ├── bin/python, pip
    └── lib/python3/site-packages/
        ├── app/                 ← python-server 源码 (cp from /usr/src/python-server/app/)
        ├── app/_app_logging/    ← 原 app/logging/ rename (避免 shadow stdlib)
        ├── browser_sdk/         ← browser-sdk 源码
        └── vendors/             ← bytedlogger, openhands, openhands_aci

/home/x/
├── .npm-global/                ← npm install -g 全局包
│   ├── bin/mcp-server-browser, chrome-devtools-mcp, claude-code, pm2
│   └── lib/node_modules/...
├── .mcp2rest/
│   └── config.yaml             ← mcp2rest 服务发现 (chrome-devtools)
├── .daemon/                    ← sandbox_guard (no-root)
│   ├── sandbox_guard.py        ← 入口 (from sandbox_reconcile.cli import main)
│   ├── sandbox_reconcile/      ← 守护逻辑
│   ├── scripts/                ← skill 批处理
│   ├── events/ locks/ apps/ work/ state/
│   └── config.json             ← 故意不 baked (运行时由 deploy 注入)
├── .pm2/                       ← pm2 daemon sockets
├── .config/
│   ├── fcitx5/profile + config ← pinyin 默认 + Ctrl+Shift 触发
│   └── browser/                ← Chrome Default profile
├── .claude-code-router/        ← CCR 配置
└── .local/share/daytona/      ← daytona workdir

/etc/environment                ← ENABLE_CJK_IME / DISABLE_JUPYTER / DISABLE_CODE / DISPLAY=:99 / DBUS_SESSION_BUS_ADDRESS ...

/tmp/
├── runtime-x/                  ← XDG_RUNTIME_DIR (chown x:x 700)
├── dbus-session-bus            ← dbus-daemon unix socket
└── daytona-daemon.log / daytona-entrypoint.log

/var/log/
├── aio-sandbox/                ← 老 LOG_DIR
└── gem/
    ├── fcitx5.log, daytona.log, ...
    └── daytona/                ← daytona 程序日志
```

### 5.2 进程启动顺序(supervisord 优先级)

| 优先级 | 进程 | 启动时机 | 备注 |
|---|---|---|---|
| 40 | reclaim-plugins | t=0 一次性 | K8s mount /home/x/plugins 权限修复,启动后 EXITED |
| 70 | sandbox-pm2-runtime | t=0 | pm2 daemon,user=x |
| 200 | visible-chrome (no-op) | 永不启动 | autostart=false (防御性) |
| 815 | dbus-session | Xvnc 之后 | dbus-daemon,user=x |
| 820 | fcitx5 | dbus 之后 | CJK 输入法,user=x |
| 900 | nginx, openbox, tigervnc, websocat, mcp-server-browser | X11 ready 之后 | |
| 920 | **browser (chrome)** | openbox 之后 | **race-fix**:避免黑屏 |
| - | python-server | | 由 supervisord nginx-wait.sh 等端口 |
| - | daytona | | 跟随 sandbox-pm2-runtime |

### 5.3 网络端口

| 端口 | 服务 | 用途 |
|---|---|---|
| 8080 | nginx (PUBLIC_PORT) | 主 dashboard |
| 5900 | tigervnc | VNC 桌面 |
| 5700 | websocat | noVNC ↔ VNC websocket |
| 9988 | python-server (SANDBOX_SRV_PORT) | API 后端 |
| 8888 | jupyter-lab | (DISABLE_JUPYTER=true 时禁用) |
| 8443 | code-server | (DISABLE_CODE=true 时禁用) |
| 8100 | mcp-server-browser | MCP 协议 |
| 9222 | chrome (CDP) | puppeteer 控制 |
| 28888 | mcp2rest | MCP gateway |
| 2280, 22222, 22220 | daytona internal | (可选) |

### 5.4 运行时 env (注入到 supervisord 子进程)

通过 `/etc/environment` + `supervisord.conf` 的 `environment=` 注入:

```
ENABLE_CJK_IME=true
DISABLE_CODE=true
DISABLE_JUPYTER=true
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/dbus-session-bus
DISPLAY=:99
ENV_DISPLAY=:99
LOG_DIR=/var/log/gem
PM2_HOME=/home/x/.pm2
```

### 5.5 关键运行时修复(build-time sed)

| 文件 | 修改 |
|---|---|
| `/opt/gem/supervisord/*.conf` | `%(ENV_USER)s` → `x`, `%(ENV_DISPLAY)s` → `:99`, `%(ENV_LOG_DIR)s` → `/var/log/gem` |
| `/opt/gem/supervisord/browser.conf` | `priority=100` → `920` (race fix) |
| `/opt/gem/supervisord/dbus-session.conf` | 替换为真实 dbus-daemon launcher (不是 noop stub) |
| `/opt/gem/supervisord/fcitx5.conf` | autostart=true, priority=820 |
| `/opt/gem/supervisord.conf` | 注入 `environment=DISPLAY=":99"` 等 |
| `/etc/environment` | 注入 `ENABLE_CJK_IME` / `DISABLE_JUPYTER` / `DISABLE_CODE` |
| `/etc/profile.d/npm-global.sh` | `export PATH="/home/x/.npm-global/bin:$PATH"` |
| `/home/x/.config/fcitx5/config` | Ctrl+Shift_L/R 触发键 |
| `/home/x/.mcp2rest/config.yaml` | `package: chrome-devtools-mcp` (不带 @latest) |
| `/opt/server-venv/lib/python3/site-packages/app/logging/` | rename → `app/_app_logging/` (避免 shadow stdlib) |
| `/opt/server-venv/lib/python3/site-packages/app/cli.py` | `loop: uvloop` → `asyncio` (uvloop .so 0 字节 workaround) |

---

## 6. 权限 / no-root 改造说明

### 6.1 原始状态

开源 `aio-sandbox` 启动后:
- supervisord 跑在 root
- `daytona`, `pm2`, `mcp2rest`, `chrome` 等子进程默认 `user=root`
- 大量 `/home/x/*` 目录被 root 创建,后续 user x 写时 EACCES
- mcp2rest 启动后无法写 `~/.mcp2rest/pm2.ecosystem.config.js`

### 6.2 v13-fix9.1+ 改造

1. **创建 user x** (uid/gid 1000),删除默认 ubuntu 用户
2. **每个 [program:*] 加 `user=x`**,supervisord (PID 1) 仍 root
3. **build-time 预创建 `/home/x/.{mcp2rest,pm2,npm,claude,claude-code-router}`** ,chown 1000:1000
4. **runtime 修复**:
   - `reclaim-plugins.conf` priority=40:K8s mount 进来的 plugins 目录强制 chown
   - `sandbox-pm2-runtime.conf`:pm2 daemon 启动前用 `su - x` 触发 `pm2 ping`,确保 .pm2/rpc.sock 是 x:x
5. **daytona 切换 user=x**,4 个 DAYTONA_* 环境变量注入,日志换到 `/var/log/gem/daytona/`
6. **dbus-session 切换 user=x**,socket 绑到 `/tmp/dbus-session-bus`,XDG_RUNTIME_DIR 绑到 `/tmp/runtime-x/`
7. **fcitx5 切换 user=x**,`/etc/profile.d/` 注入 `GTK_IM_MODULE=fcitx` 等,确保 chrome GTK 进程能连

### 6.3 启动顺序 race fix(关键)

**问题**:chrome 启动时如果 Xvnc 还没 bind `:99`,chrome 会创建 InputOnly root window,VNC 看到黑屏。

**解决**:
- supervisord 按 priority 升序启动 (低先)
- chrome priority=100 → 920(openbox=900 之后)
- dbus-session priority=815(Xvnc 之后)
- fcitx5 priority=820(dbus 之后)
- 实测 cold-start 45 秒,无黑屏。

---

## 7. 常见问题

### Q1: `docker build` 失败:apt-get update 502 Bad Gateway

CN 镜像在 26.04 上偶发 502。Dockerfile 已经做了:
- `apt-get update` 5 次重试
- `apt-get install` 5 次重试,失败 `|| echo WARN_apk_install_failed`
- 单包缺失 → 网络兜底 → 继续 build

如果持续失败,改 `APT_MIRROR=...` build-arg 试其他 mirror:
```bash
docker buildx build -f Dockerfile.final \
    --build-arg APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/ubuntu \
    -t aio-sandbox:final-test --load .
```

### Q2: pip 安装 .so 0 字节(SIGSEGV)

这是 `pip install` 在 Windows docker-desktop overlayfs 上的 Errno 22 bug。

Dockerfile 已经处理:
- `sweep-so.py` 在 build 时扫描所有 .so,删 0 字节的
- `build-fix-so.sh` 重新装关键 C-extension (pydantic-core 2.46.4)
- venv-patch.sh 在运行时也做相同检查 (defense in depth)

如果还有问题,加 `--no-cache-dir` 重 build,或检查 `wheels/` 里 pydantic-core 的完整性。

### Q3: Chrome 启动黑屏

1. 确认 `priority=920` 在 `/opt/gem/supervisord/browser.conf` 还在
2. 确认 dbus-session 在 815 启动,fcitx5 在 820
3. `docker exec aio-final xwininfo -root -tree` 看 chrome 是否有 root window
4. `supervisorctl restart openbox && supervisorctl restart browser` 手工触发

### Q4: mcp2rest 启动后 EACCES

- 确认 `/home/x/.mcp2rest/` 是 x:x(在 build 时已 chown)
- 确认 `/home/x/.npmrc`, `/home/x/.npm/`, `/home/x/.npm-global/` 整树都是 x:x
- `docker exec aio-final ls -la /home/x/.mcp2rest`

### Q5: jupyter 还是启动了

- 确认 `DISABLE_JUPYTER=true` env 在 `/etc/environment`
- `docker exec aio-final env | grep JUPYTER`
- `docker exec aio-final cat /opt/gem/supervisord.conf | grep -A3 supervisord`

### Q6: build 太慢,能离线 build 吗

能,但需要把 4 GB 镜像(基础) + 全部 apt/wheels/npm/rust tarball 都预下载到 host。简单做法:在能联网的机器上跑 `prepare-all.sh`,把 `docker/context/{apt-archives,wheels,npm-tgz,rustup-pre,cargo-vendored,bin,dist}` 7 个目录 rsync 到离线机器的同一位置。

### Q7: 镜像太大(4 GB),能瘦身吗

参考 08-IMAGE-SIZE-ANALYSIS.md(原始版本)做 P0-P4 瘦身。当前 final-version 已经做了 P0-P2。

可继续做的:
- 多阶段构建,把 `node:22-slim` 替换为 `alpine` (~50MB → 5MB)
- 删除 `noto-cjk-extra` 字体 (节省 ~500MB)
- `dpkg --purge` 删更多 -dev 包 (gcc-15 / build-essential / cmake 等),但 base image 已经是 ubuntu 26.04 minimal,空间有限

---

## 8. 离线 / 在线分析

### 8.1 严格离线步骤(50%)

build 时**完全不需要网络**(前提是 `docker/context/` 资源齐全):

- Stage 1-4 (uv / aio-build / static-assets / ve-build):全 COPY + local install
- §2 apt 大列表 (`apt-archives/*.deb`)
- §9 chrome / noVNC / websocat
- §10 wheels 离线 (`pip install --no-index --find-links`)
- §11 rootfs COPY
- §12 source trees COPY
- §13 aio / static-assets / ve 二进制 COPY --from
- §14 rootfs COPY
- §14q guard COPY

### 8.2 离线优先 + 在线兜底(25%)

策略是先试离线,失败 fallback 到 CN mirror 网络(失败 `|| echo WARN`):

- §2 apt-get update (5 次重试)
- §2 apt 大列表 (5 次重试,`libcrypt-dev` 等少数包走在线)
- §2a im-config
- §3 locale (`apt-get install locales`)
- §7 curl/unzip/ncurses-term
- §7b Node.js REPL server npm install
- §10 dev.txt pip install
- §2b oras(GitHub releases,纯在线)
- §2c fastmcp pip(TUNA)
- §7 fnm + node 22 (fnm 走 GitHub,node 22 走 npm mirror)

### 8.3 必走在线(25%)

无离线替代,build 时必须有网络:

- oras 二进制 (GitHub releases)
- fnm 二进制 (GitHub releases) — 6 MB
- node 22 二进制 (npm mirror) — 50 MB
- ~~node 24 二进制~~ — **已注释掉,不再下载**
- §15 全局 npm install (`@anthropic-ai/claude-code`, `playwright`, 等) — ~500 MB
- §16 requests pip (TUNA) — 几 KB

### 8.4 完全离线 build 的额外步骤

要把 §8.2 和 §8.3 也变离线,需要补:

```bash
# 新建 docker/context/fnm/fnm-linux.zip
# 新建 docker/context/npm-tgz/globals/  (预 pack mcp2rest / claude-code / pm2 / playwright / ...)
# 修改 Dockerfile.final,从 context 引用而不是 npm install
```

预计增加 ~600 MB 离线资源,可以做到 100% 离线 build。

---

## 维护

- 修改 `Dockerfile.final` 后,只需 `docker buildx build ... --load .` 重新 build
- 修改 `docker/context/rootfs/*` 后同样重新 build
- 修改 `docker/context/{aio,static-assets,browser-sdk,python-server,repl-servers,bin,dist}/` 后,要么重新 `prepare-*.sh`,要么直接修改源码
- `docker/context/{apt-archives,wheels,npm-tgz,rustup-pre,cargo-vendored}/` 是 build 输入,改 Dockerfile 时同步