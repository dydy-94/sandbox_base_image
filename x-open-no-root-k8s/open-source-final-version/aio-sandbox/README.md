# aio-sandbox 改造记录

最终构建文件：`Dockerfile.final`（构建：`docker buildx build -f Dockerfile.final -t aio-sandbox:final-test --load .`）

## 1. 非 root 运行（全部进程为 x 用户）

- supervisord 自身：`supervisord.conf` 顶层 `user=x`，pid/log/socket 在 `/home/x/.run/`
- 所有子程序（browser/vnc/nginx/openbox/dbus/fcitx5/websocat/gost/mcp/nodejs_repl/daytona/pm2）均 `user=x`
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

## 2a. sudo 提权限制（需密码）

- x 用户**可以 sudo 但必须输入密码**，移除了默认的 `NOPASSWD:ALL`
- sudoers 规则：`x ALL=(ALL) ALL`（默认需密码验证）
- 密码：默认 `x123456`，可用 `docker run -e X_USER_PASSWORD=xxx` 覆盖
- 效果：浏览器页面/粘贴的命令以 x 身份执行时，无法静默 `sudo -i` 提权（`sudo -n` 直接失败）；所有 sudo 使用记录在 /var/log/auth.log
- 注意：x 用户验证密码成功后有标准 sudo 15 分钟凭据缓存，属正常行为

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

## 7. 离线化 & 启动优化（前期已完成）

- oras / fnm-node / node repl / global npm 全部离线预置，构建期固定权限
- 运行时不再做重 chown（构建时固化），启动秒级完成

## 8. 验证方式

```bash
docker run -d --name aio-v2 -p 15000:8080 aio-sandbox:final-test
# 访问 http://localhost:15000/  dashboard（VNC 面板 + 无 terminal/browser 菜单）
docker exec aio-v2 supervisorctl -c /opt/gem/supervisord.conf status
docker exec aio-v2 ps -ef   # 确认所有进程 user=x
docker exec aio-v2 date     # 确认 CST 时区
```
