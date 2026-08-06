# AIO Sandbox — 自定义构建总索引

> 本目录是从开源 `aio-sandbox-2.0.1`（逆向重建版）一路改造而来的自定义构建。
> 这份 README 是所有文档的入口，按时间顺序列出 10 份改造文档，并总结原版开源"起不来"的根因。

---

## 一、文档索引（按时间顺序）

| # | 文件 | 阶段 | 主题 |
|---|---|---|---|
| 01 | [01-README-reconstruct.md](01-README-reconstruct.md) | 起点 | 原版开源 v2.0.1 的逆向重建说明，记录"Build & runtime fixes" |
| 02 | [02-OFFLINE-BUILD-FIX.md](02-OFFLINE-BUILD-FIX.md) | v9 → v10 | 第一份大文档，记 v9 runtime 装 pip 的痛点和 v10 offline 化目标，列 13 项关键修复 |
| 03 | [03-README-v10-rebuild.md](03-README-v10-rebuild.md) | v10 重构 | 把 v9 → v10 的 11 个独立 bug 编号、归类 |
| 04 | [04-README-v10-final.md](04-README-v10-final.md) | v10 收敛 | 真正"冷启动即用"的修复清单，提出"四层职责交叉"根因 |
| 05 | [05-README-cold-start-architecture.md](05-README-cold-start-architecture.md) | v10 架构审计 | 对 04 的复盘，定义"源文件即终态"原则 |
| 06 | [06-CN-MIRROR-README.md](06-CN-MIRROR-README.md) | CN 镜像构建指南 | 国内网络环境下的构建手册 |
| 07 | [07-CHANGES-CN-MIRROR.md](07-CHANGES-CN-MIRROR.md) | CN 镜像改动记录 | 配套 06 的改动清单，含 wheel 截断/post-inst.sh |
| 08 | [08-IMAGE-SIZE-ANALYSIS.md](08-IMAGE-SIZE-ANALYSIS.md) | v10-offline-v22 | 体积分析报告（~7.4GB） |
| 09 | [09-README-custom-build.md](09-README-custom-build.md) | root-daemon 合入 | x 用户、daytona、招行私有源等定制（特殊需求，非通用） |
| 10 | [10-README-image-slim.md](10-README-image-slim.md) | P0-P4 瘦身 | 镜像大小优化文档 |

### 阅读建议

- **第一次接手**：先读 01 了解原版开源是什么，再读 04 理解"为什么原版起不来"
- **要构建镜像**：读 06 + 07（CN 环境）或 02（通用 offline）
- **要优化大小**：读 08 + 10
- **特殊定制（x 用户/daemon）**：读 09

---

## 二、原版开源为什么起不来（根因总结）

原版 `aio-sandbox-2.0.1`（`D:\AIO 新镜像打造\open-source-zip`）本身是**逆向重建版**（从运行的 `ghcr.io/agent-infra/sandbox` 容器里把文件扒出来重建 Dockerfile，原版上游 Dockerfile 从未公开）。它自己的 [01-README-reconstruct.md §Build & runtime fixes](01-README-reconstruct.md) 就承认需要一堆修复才能跑。

按"症状 → 根因 → 影响"分类：

### A. 启动直接失败（容器起不来 / PID 1 退出）

| # | 根因 | 证据 |
|---|---|---|
| A1 | **`/opt/application/run.sh` 没 shebang** | 原版只有一行 `/opt/gem/run.sh`，没 `#!/bin/bash`。`CMD ["/opt/application/run.sh"]` 是 exec form，调 `execve(2)`，没有 shebang 就直接 ENOEXEC |
| A2 | **`gettext-base` 没装 → `envsubst` 不存在** | run.sh 用 `envsubst` 渲染 nginx 配置。原版 apt 列表漏了这个包，启动直接 `envsubst: command not found`，exit 127。见 [03 Bug #11](03-README-v10-rebuild.md) |
| A3 | **缺关键 ENV 变量** | supervisord 配置里有 `%(ENV_LOG_DIR)s`、`%(ENV_DISPLAY)s`、`%(ENV_USER_UID)s` 等，env 没设就 abort。原版 README 列了 missing: `LOG_DIR`、`DISPLAY`、`DISPLAY_DEPTH`、`USER_UID/USER_GID` |

### B. 起来但 nginx 起不来（全部面板 502/404）

| # | 根因 | 证据 |
|---|---|---|
| B1 | **run.sh 的 envsubst 在端口 export 之前跑** | [04 §1.1](04-README-v10-final.md) 点名的"四层职责交叉"根因。envsubst 时 `${BROWSER_REMOTE_DEBUGGING_PORT}` 还是空 → 渲染出 `proxy_pass http://127.0.0.1:;` → nginx emerg FATAL |
| B2 | **`WAIT_PORTS` 写错** | 原版 README：原版用 `8079,8091`，但 8079 没人绑，nginx-wait.sh 永远阻塞，nginx 永远不 start。修成 `8091` 才对 |
| B3 | **`WAIT_TIMEOUT` 没默认值** | nginx-wait.sh `if [ $elapsed -ge $WAIT_TIMEOUT ]`，变量空时 bash 报 `unary operator expected`，nginx FATAL。见 [03 Bug #3](03-README-v10-rebuild.md) |
| B4 | **nginx upstream 端口和实际服务对不上** | 4 个 conf 文件 `proxy_pass` 用 `8091`（python-server 旧端口）/`8200`（code-server 旧端口），但实际服务在 `9988`/`8443` |
| B5 | **noVNC 没 `index.html`** | 原版只发 `vnc.html`，但 aio dashboard 引用的是 `/vnc/index.html?autoconnect=true...` → 404 |

### C. python-server 起不来（API/terminal 全挂）

| # | 根因 | 证据 |
|---|---|---|
| C1 | **`requirements/server.txt` 是空文件** | [02 §3.1 改动 #1](02-OFFLINE-BUILD-FIX.md)："server.txt 是空文件 → 等价于「什么也没装」→ python-server 启动就 ModuleNotFoundError"。47 个 runtime 依赖一个没声明 |
| C2 | **`app/logging` 与 stdlib `logging` 重名** | `from app.logging import setup_logger` 被 Python 解析成 import stdlib，触发 `ModuleNotFoundError: No module named 'logging'` |
| C3 | **`*.dist-info` 整目录被删** | 早期 cleanup 脚本把 METADATA 也删了，pydantic `importlib.metadata.version('email-validator')` 找不到 → FATAL |
| C4 | **server.txt 把版本 pin 锁太死** | `uvicorn<0.32.0,>=0.30.0`、`fastapi<0.110` 这些版本在 cp313 wheel 上 PyPI 已经没有了，pip 直接 ResolutionImpossible |
| C5 | **Windows overlayfs bug 让 .so 静默丢失** | [06 §5](06-CN-MIRROR-README.md)：pip `os.replace` 在 docker-desktop overlayfs 上 Errno 22 静默失败，dist-info 在但 .so 是 0 字节，运行时 SIGSEGV |

### D. VNC / Chrome 起不来（VNC 黑屏 / browser-ui 卡 reconnect）

| # | 根因 | 证据 |
|---|---|---|
| D1 | **Chrome 150 CDP 端口不绑** | Chrome 150 安全策略变更，`--remote-debugging-port` 必须配合 `--user-data-dir` 指向非默认路径，否则端口静默不绑 |
| D2 | **`--headless=new` 不写 X11** | headless 模式渲染到离屏缓冲区，VNC 看到的是 openbox 空桌面 |
| D3 | **`--remote-allow-origins=*` 缺失** | Chrome 111+ 拒绝所有 CDP WS origin，browser-ui 一直 "Reconnecting (5/10)..." |
| D4 | **`agent-browser-real` binary 不在镜像里** | 原版 supervisord 列了 `agent-browser.conf`，但 daemon binary 从未 baked → X11 桌面没 chrome 窗口 |
| D5 | **缺 `BROWSER_EXECUTABLE_PATH` / `BROWSER_COMMANDLINE_ARGS`** | 原版 README：start-browser.sh `exec`s an empty path，Chrome 永远不启 |
| D6 | **Chrome 缓存了用户 profile** | 每次启动弹 "select user / sign in" 窗口挡住 CDP，没加 `--guest` |

### E. code-server / Jupyter 起不来

| # | 根因 | 证据 |
|---|---|---|
| E1 | **code-server binary 路径不一致** | install.sh 只 symlink 到 `/usr/local/bin/code-server`，supervisord 调 `/usr/bin/code-server` → ENOENT |
| E2 | **`/home/gem/.config/code-server` 没建** | code-server EACCES，每 10 秒重启一次 |
| E3 | **`/opt/jupyter/` 目录没建** | supervisord.jupyter.conf 配 `chdir=/opt/jupyter`，build 时没 mkdir → ENOENT FATAL |
| E4 | **`CODE_SERVER_PORT` / `JUPYTER_LAB_PORT` 没 export** | envsubst 渲染时变空字符串，supervisord-wirifier.py 又不可靠 |

### F. 前端资源 404

| # | 根因 | 证据 |
|---|---|---|
| F1 | **static-assets COPY 路径错** | build stage 输出到 `/out/var/www/app/static/sandbox/`，Dockerfile COPY 到 `/opt/static-assets/`，但 nginx `root /var/www/app` → xterm.js 404 → terminal 黑屏 |
| F2 | **整个 `/static/sandbox/` 树缺失** | 原版 README：rootfs 完全没有这棵树（xterm、swagger-ui、clipboard、browser-ui），页面加载但 JS 全 404 |
| F3 | **`ncurses-term` 没装** | bashrc 里 `export TERM=xterm-256color`，但 `/usr/share/terminfo/x/xterm-256color` 不存在 → `clear: unknown terminal type` |

### G. 构建/编辑器副作用

| # | 根因 | 证据 |
|---|---|---|
| G1 | **CRLF 行尾** | Windows PowerShell 编辑后 `.sh`/`.conf` 全是 `\r\n`，bash 不接受 `set -e\r`。[03 Bug #9](03-README-v10-rebuild.md) 一次扫了 275 个文件 |
| G2 | **`fnm` 没 `mkdir /opt/nodejs`** | 原版 README：symlink 创建前目录不存在 → build 失败 |
| G3 | **pip 缺 `--break-system-packages`** | Ubuntu 26.04 的 apt python 自带 PEP-668 `EXTERNALLY-MANAGED`，pip 拒绝装 |

### 一句话总结

**原版开源 v2.0.1 起不来的根本原因不是某个 bug，而是"逆向重建不完整 + 运行时配置生成时序错乱"两层叠加：**

1. **逆向重建丢了一堆运行时必需品** — shebang、ENV 变量、`server.txt` 内容、前端静态资源、`BROWSER_EXECUTABLE_PATH`、`gettext-base`、`ncurses-term`、noVNC `index.html`……这些在原版上游的预构建镜像里是存在的，但重建 Dockerfile 没把它们补齐。
2. **`run.sh` 的 envsubst 时序错乱** — 端口默认值 export 在 envsubst 之后，把所有 nginx conf 渲染成空端口，nginx FATAL，连锁让全部面板不可用。这是 [04](04-README-v10-final.md) 点出的"四层职责交叉"核心。
3. **Chrome 150 行为变更没适配** — `--user-data-dir` / `--remote-allow-origins=*` / 不用 `--headless=new` 这三个新约束没补，VNC 黑屏 + CDP 不绑 + browser-ui 卡 reconnect 三连。
4. **Windows + CN 镜像环境雪上加霜** — CRLF、overlayfs .so 丢失、wheel 截断，都是原版假设 Linux + 公网环境下不会遇到的问题。

这 10 份 README 实际上是在逐层补这 30 来个独立缺陷，最终在 v10-final/v16 收敛到"冷启动即用"。

---

## 三、当前镜像组件

| 组件 | 端口 | 说明 |
|---|---|---|
| nginx | 8080 | 主 HTTP router + 反向代理 |
| python-server | 9988 | AI/MCP/sandbox API 后端 |
| code-server | 8443 | 内嵌 VSCode |
| jupyter-lab | 8888 | JupyterLab（已禁用） |
| tigervnc + openbox | 5900, :99 | VNC 桌面 |
| websocat | 5700 | noVNC websocket 桥 |
| google-chrome (CDP) | 9222 | Chrome Debug Protocol |

---

## 四、快速上手

```bash
# 构建（从 docker/ 目录运行）
docker buildx build -f Dockerfile.offline -t aio-sandbox . --load

# 运行
docker run -d --name aio -p 18001:8080 --shm-size=1g aio-sandbox
sleep 30  # 冷启动约 30 秒

# 测试
curl -m 10 -sL -w "code=%{http_code}\n" http://localhost:18001/           # 200 dashboard
curl -m 10 -sL -w "code=%{http_code}\n" http://localhost:18001/v1/ping    # 200
curl -m 10 -sL -w "code=%{http_code}\n" http://localhost:18001/vnc/index.html  # 200
```

面板入口：
- Dashboard: `http://localhost:18001/`
- Terminal: `http://localhost:18001/terminal/`
- Code Server: `http://localhost:18001/code-server/`
- VNC: `http://localhost:18001/vnc/index.html?autoconnect=true`
