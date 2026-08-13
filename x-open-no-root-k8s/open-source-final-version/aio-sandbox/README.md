# aio-sandbox 改造记录

最终构建文件：`Dockerfile.final`（构建：`docker buildx build -f Dockerfile.final -t aio-sandbox:final-test --load .`）

## 1. 非 root 运行（全部进程为 x 用户）

- supervisord 自身：`supervisord.conf` 顶层 `user=x`，pid/log/socket 在 `/home/x/.run/`
- 所有子程序（browser/vnc/nginx/openbox/dbus/fcitx5/websocat/mcp/nodejs_repl/daytona/pm2）均 `user=x`
- guard 运行时模板统一用 `%(ENV_USER)s`（=x）

## 2. 服务开关（run.sh 默认值）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| DISABLE_JUPYTER | true | 关闭 jupyter |
| DISABLE_CODE_SERVER | true | 关闭 code-server |
| DISABLE_BROWSER | false | chrome 默认启动（CDP 9222） |
| DISABLE_VNC | false | VNC 默认启动 |
| DISABLE_BROWSER_UI | true | 仅控制 dashboard /browser-ui 面板，chrome 进程不受影响 |
| DISABLE_MCP_BROWSER | true | 关闭 MCP browser |
| DISABLE_OPENCODE | true | 关闭 opencode |

`browser.conf` 的 `autostart` 改为 `%(ENV_AUTOSTART_BROWSER)s` 环境变量化。

## 2a. x 完全无 sudo（完全非特权）

- x 不在 sudo 组、无任何 sudoers 规则，容器内无法提权（docker root 模式与 k8s runAsUser:1000 行为一致）
- 系统级安装（apt）不可用；语言/依赖包走用户级安装（pip --user、npm 本地 node_modules、解压到 ~/go、~/jdk 等）
- 浏览器页面/粘贴的命令以 x 身份执行时，`sudo` 直接拒绝，无法静默提权
- bwrap 的 userns 内核参数由平台授予（k8s securityContext.sysctls / privileged），build 时仅在可写时尝试，运行时不使用 sudo

## 3. dashboard 面板控制（index.html）

- terminal / browser / opencode 面板：`enabled: false`（右上角菜单隐藏、不显示）
- `loadSavedState()` 恢复 localStorage 时跳过 `enabled:false` 面板（防止旧状态把 terminal 拉回显示）
- 默认只显示 VNC 面板
- 入口级关闭（不只 UI）：`DISABLE_TERMINAL_UI=true`（默认）删除 nginx `/terminal` 路由；`DISABLE_OPENCODE=true` 删除 `/opencode`（其 302 直通 /terminal）；`DISABLE_MCP_BROWSER=true` 删除 `/mcp`、`/v1/mcp`。实测仅 VNC 可达
- 重启健壮性：run.sh 清理 stale `/tmp/.X*-lock`（docker restart 强杀 Xvnc 后 lock 残留会引发 tigervnc FATAL → chrome 无 DISPLAY → nginx 起不来的级联故障）

## 4. 浏览器跳过初始化 & 登录

- 新增 `BROWSER_NO_FIRST_RUN`（默认 true）：
  - run.sh 拼接 chrome 参数 `--no-first-run --no-default-browser-check --disable-sync`
  - 预置 profile：`browser.first_run_beacon`、`profile.gaia_info`、`profile.exit_type=Normal`、`signin.allowed=false`、`sync_promo.show_on_first_run_allowed=false`
  - 设为 false 可恢复初始化流程
- `preferences.json` 静态预置以上键作为基础

## 5. Browser 环境变量修复（参数重复）

- 根因：supervisord.conf 顶层硬编码 `BROWSER_EXTRA_ARGS` 覆盖了 run.sh 运行时动态计算的值
- 修复：Dockerfile 14p 段只注入 `DISPLAY` 和 `DBUS_SESSION_BUS_ADDRESS`，BROWSER_EXTRA_ARGS 完全由 run.sh 动态拼接（`--lang`、`--time-zone-for-testing`、proxy 参数、BROWSER_NO_FIRST_RUN）
- 效果：chrome 参数不再重复/空值

## 6. 时区（中国上海）

- `ENV TZ=Asia/Shanghai`
- `/etc/localtime` 软链 + `/etc/timezone` 写入
- 注意：squash 阶段（Stage 6）`COPY --from=builder / /` 只带文件，**ENV 元数据不传递**，所以 final stage 必须显式再声明一次 `ENV TZ=Asia/Shanghai`（builder 与 final 两处都已添加）

## 7. 离线化 & 启动优化

- oras / fnm-node / node repl / global npm 全部离线预置，构建期固定权限
- 运行时不再做重 chown（构建时固化），启动秒级完成

### 7.1 离线包下载流程（构建前必须执行）

所有大体积下载（apt .deb、pip wheels、npm tarballs、daytona 二进制、fnm+node）在**宿主机构建前**完成，Dockerfile 构建时只 COPY 这些预置资产并离线安装（`apt-get install --no-download`、`pip install --no-index --find-links`、`npm install --prefer-offline`）。

**一键下载全部离线资产：**

```bash
bash docker/context/prepare-all.sh
```

内部按依赖顺序执行 5 步（每步失败会打印日志路径，可用 `SKIP_<NAME>=1` 跳过）：

| # | 脚本 | 产出目录 | 内容 |
|---|---|---|---|
| 1 | `prepare-apt-archives.sh` | `docker/context/apt-archives/` | 769+ 个 apt .deb（含 chrome/noVNC/websocat/code-server） |
| 2 | `prepare-wheels.sh` | `docker/context/wheels/` | python-server 运行期 pip wheels |
| 3 | `prepare-npm.sh` | `docker/context/npm-tgz/` | aio / static-assets npm tarballs |
| 4 | `prepare-daytona.sh` | `docker/context/bin/`、`docker/context/dist/libs/` | daytona daemon + computer-use 插件（必须是 Linux ELF 版） |
| 5 | `prepare-fnm-node.sh` | `docker/context/` | fnm + node 22 tarball |

下载完成后打印资产清单（文件数 + 大小）与日志位置（`docker/context/.prepare-logs/`）。

**镜像源说明：**
- 默认走 CN 镜像（TUNA / aliyun / npmmirror），失败自动回退上游官方源
- 有内网私有源时用环境变量覆盖：`APT_MIRROR` / `NPM_REGISTRY` / `PIP_INDEX_URL`（如 cmbchina jaf 源）
- Windows 宿主可替代：`prepare-npm.ps1`（npm 部分）

**下载完成后构建：**

```bash
cd docker/../..   # 即 aio-sandbox 目录
docker buildx build -f Dockerfile.final -t aio-sandbox:final-test --load .
```

> ⚠️ 注意：构建期仍有一小部分 `apt-get install` 作为非离线 fallback（如 libcrypt-dev 不在离线缓存中），需要构建机网络可达 CN 镜像。完全断网构建请先跑 `preflight-wheels.sh` 确认 wheels 完整性。

## 8. 验证方式

```bash
docker run -d --name aio-v2 -p 15000:8080 aio-sandbox:final-test
# 访问 http://localhost:15000/  dashboard（VNC 面板 + 无 terminal/browser 菜单）
docker exec aio-v2 supervisorctl -c /opt/gem/supervisord.conf status
docker exec aio-v2 ps -ef   # 确认所有进程 user=x
docker exec aio-v2 date     # 确认 CST 时区
```
