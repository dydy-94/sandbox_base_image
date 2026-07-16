# Daytona Daemon 鉴权改造 - JWT 本地校验版

## 1. 概述

`daemon-new` 取消原先"调用外部 URL 鉴权接口"的方案，改为 **本地解析请求头中的 `id-token` (JWT) 并进行签名与声明校验**。
整个鉴权流程不依赖任何外部服务，单进程内完成，部署更简单、性能更高、可靠性更好。

## 2. 鉴权流程

```
HTTP 请求到达
   │
   ▼
1. 读取环境变量 / 配置文件
   │
   ▼
2. 鉴权是否关闭 (DAYTONA_AUTH_ENABLED=false)？
   ├── Yes ──> 直接放行，日志: auth_result=disabled
   └── No  ──> 进入 3
   │
   ▼
3. 请求路径是否在白名单 (DAYTONA_AUTH_EXCLUDE_PATHS)？
   ├── Yes ──> 直接放行，日志: auth_result=skipped
   └── No  ──> 进入 4
   │
   ▼
4. 解析请求头中的 JWT (默认 header: id-token)
   ├── 头部缺失 ──> 返回 401，日志: auth_result=missing_token
   └── 有 token ──> 进入 5
   │
   ▼
5. 解析并校验 JWT
   ├── 解析失败 (格式错误) ──> 401，auth_result=invalid_token
   ├── 签名校验失败      ──> 401，auth_result=invalid_signature
   ├── 过期 (exp)        ──> 401，auth_result=expired
   ├── 尚未生效 (nbf)    ──> 401，auth_result=not_yet_valid
   ├── 签发方不匹配 (iss) ──> 401，auth_result=invalid_issuer
   └── 全部通过          ──> 放行，日志: auth_result=passed
```

## 3. 配置方式

### 3.1 配置文件（推荐）

查找顺序（找到第一个存在的文件即用）：

1. `$HOME/.daemon/auth.yaml`
2. `/home/x/.daemon/auth.yaml`
3. `/etc/daytona/auth.yaml`

格式参考 `auth.yaml.example`：

```yaml
auth:
  enabled: true
  id_token_header: "id-token"
  jwt_algorithm: "HS256"
  jwt_secret: "your-shared-secret-please-change"
  jwt_issuer: "daytona-runner"
  jwt_audience: "daytona-daemon"
  exclude_paths:
    - /version
    - /health
    - /user-home-dir
    - /work-dir
  failure_status: 401
```

### 3.2 环境变量（优先级高于配置文件）

| 环境变量 | 类型 | 默认值 | 说明 |
|----------|------|--------|------|
| `DAYTONA_AUTH_ENABLED` | bool | `false` | 总开关：`true` 启用本地 JWT 校验 |
| `DAYTONA_AUTH_ID_TOKEN_HEADER` | string | `id-token` | 携带 token 的请求头名 |
| `DAYTONA_AUTH_VALIDATE_SIGNATURE` | bool | `false` | 是否校验签名（默认关闭） |
| `DAYTONA_AUTH_VALIDATE_EXPIRATION` | bool | `false` | 是否校验 `exp` / `nbf`（默认关闭） |
| `DAYTONA_AUTH_VALIDATE_ISSUER` | bool | `false` | 是否校验 `iss`（默认关闭） |
| `DAYTONA_AUTH_VALIDATE_AUDIENCE` | bool | `false` | 是否校验 `aud`（默认关闭） |
| `DAYTONA_AUTH_JWT_ALGORITHM` | string | `HS256` | 签名算法（仅在签名校验开启时使用） |
| `DAYTONA_AUTH_JWT_SECRET` | string | - | 对称算法（HS*）使用的共享密钥 |
| `DAYTONA_AUTH_JWT_PUBLIC_KEY` | string | - | 非对称算法（RS*/ES*）使用的公钥（PEM 或 `@/path/to/key.pem`） |
| `DAYTONA_AUTH_JWT_ISSUER` | string | - | 期望的 `iss` 声明（仅在 issuer 校验开启时使用） |
| `DAYTONA_AUTH_JWT_AUDIENCE` | string | - | 期望的 `aud` 声明（仅在 audience 校验开启时使用） |
| `DAYTONA_AUTH_EXCLUDE_PATHS` | string | `/version,/health,/user-home-dir,/work-dir` | 白名单（逗号分隔） |
| `DAYTONA_AUTH_FAILURE_STATUS` | int | `401` | 鉴权失败返回的 HTTP 状态码 |
| `DAYTONA_AUTH_CLOCK_SKEW_SEC` | int | `30` | 时钟偏移容忍（秒，仅在 expiration 校验开启时使用） |

> **按需校验原则**：
> - 4 个 `DAYTONA_AUTH_VALIDATE_*` 开关默认全部为 `false`，daemon 仅做 base64+JSON 解析，不做任何有效性断言。
> - 推荐至少开启 `DAYTONA_AUTH_VALIDATE_SIGNATURE=true`，否则任意人能伪造 token。
> - 时间校验（`exp` / `nbf`）默认不开启，需要 token 自带过期机制时显式打开。

> **注意**：
> - 关闭鉴权时（`DAYTONA_AUTH_ENABLED=false`），daemon 不会对 token 做任何检查。
> - 算法为 HS* 时只需配置 `DAYTONA_AUTH_JWT_SECRET`；为 RS*/ES* 时只需配置 `DAYTONA_AUTH_JWT_PUBLIC_KEY`。
> - 环境变量、配置文件 **不要同时混用密钥**，避免被日志泄露。

## 4. 按需校验逻辑（重点）

本节描述 daemon 对 JWT 的具体校验行为。

### 4.1 校验矩阵

| 校验项 | 默认值 | 开启环境变量 | 开启后行为 |
|--------|--------|--------------|------------|
| 签名校验 | ❌ 关闭 | `DAYTONA_AUTH_VALIDATE_SIGNATURE=true` | 用配置的密钥验证签名 |
| 有效期校验（exp/nbf） | ❌ 关闭 | `DAYTONA_AUTH_VALIDATE_EXPIRATION=true` | 检查 token 是否过期/尚未生效 |
| 签发方校验（iss） | ❌ 关闭 | `DAYTONA_AUTH_VALIDATE_ISSUER=true` | 比对 `iss` 与 `DAYTONA_AUTH_JWT_ISSUER` |
| 接收方校验（aud） | ❌ 关闭 | `DAYTONA_AUTH_VALIDATE_AUDIENCE=true` | 比对 `aud` 与 `DAYTONA_AUTH_JWT_AUDIENCE` |

**关闭所有校验项时**：daemon 仅做 base64 解码与 JSON 解析，claims 透传给业务层；
任意能访问到 daemon 的人都能伪造 token，因此生产环境至少需开启 `validate_signature`。

### 4.2 典型部署场景

**场景 1：内部受信网络 + 仅作审计标识**
```bash
# 仅解析 token，不做任何校验（默认行为）
DAYTONA_AUTH_ENABLED=true
# 业务层从 claims 中读取 sub 字段作为操作人标识
```

**场景 2：对称密钥签名（推荐）**
```bash
DAYTONA_AUTH_ENABLED=true
DAYTONA_AUTH_VALIDATE_SIGNATURE=true
DAYTONA_AUTH_JWT_ALGORITHM=HS256
DAYTONA_AUTH_JWT_SECRET="my-strong-shared-secret"
```

**场景 3：严格校验（生产环境）**
```bash
DAYTONA_AUTH_ENABLED=true
DAYTONA_AUTH_VALIDATE_SIGNATURE=true
DAYTONA_AUTH_VALIDATE_EXPIRATION=true
DAYTONA_AUTH_VALIDATE_ISSUER=true
DAYTONA_AUTH_VALIDATE_AUDIENCE=true
DAYTONA_AUTH_JWT_ALGORITHM=RS256
DAYTONA_AUTH_JWT_PUBLIC_KEY="@/etc/daytona/jwt-public.pem"
DAYTONA_AUTH_JWT_ISSUER=daytona-runner
DAYTONA_AUTH_JWT_AUDIENCE=daytona-daemon
DAYTONA_AUTH_CLOCK_SKEW_SEC=60
```

### 4.3 必须校验的声明

仅在对应环境变量开启时校验：

- `exp` (Expiration Time)：未过期
- `nbf` (Not Before)：已生效（可配置容忍时钟偏移）
- `iss` (Issuer)：等于 `DAYTONA_AUTH_JWT_ISSUER`
- `aud` (Audience)：等于 `DAYTONA_AUTH_JWT_AUDIENCE`（支持单值或数组）
- 签名：使用配置的密钥/公钥验证

### 4.4 推荐 Token Payload 示例

```json
{
  "iss": "daytona-runner",
  "aud": "daytona-daemon",
  "sub": "runner-55.40.15.144",
  "iat": 1735689600,
  "nbf": 1735689600,
  "exp": 1735693200,
  "scope": "process.execute fs.read"
}
```

### 4.3 算法支持

| 算法 | 所需密钥 |
|------|----------|
| HS256 / HS384 / HS512 | `DAYTONA_AUTH_JWT_SECRET` |
| RS256 / RS384 / RS512 | `DAYTONA_AUTH_JWT_PUBLIC_KEY` |
| ES256 / ES384 / ES512 | `DAYTONA_AUTH_JWT_PUBLIC_KEY` |

非对称密钥支持两种方式：
- 直接传 PEM 内容：`DAYTONA_AUTH_JWT_PUBLIC_KEY="-----BEGIN PUBLIC KEY-----\n..."`
- 传文件路径：`DAYTONA_AUTH_JWT_PUBLIC_KEY="@/etc/daytona/jwt.pub"`

## 5. 部署示例

### 5.1 关闭鉴权（默认行为）

```bash
docker run -d --name daytona-new -p 8080:8080 daytona-new:amd64
```

### 5.2 启用 HS256 对称密钥鉴权

```bash
# 1. 准备配置文件
mkdir -p /home/x/.daemon
cat > /home/x/.daemon/auth.yaml <<EOF
auth:
  enabled: true
  jwt_algorithm: "HS256"
  jwt_secret: "my-strong-shared-secret"
  jwt_issuer: "daytona-runner"
  jwt_audience: "daytona-daemon"
EOF

# 2. 挂载配置文件启动
docker run -d --name daytona-new -p 8080:8080 \
  -v /home/x/.daemon/auth.yaml:/home/x/.daemon/auth.yaml:ro \
  daytona-new:amd64
```

### 5.3 启用 RS256 非对称密钥鉴权

```bash
docker run -d --name daytona-new -p 8080:8080 \
  -e DAYTONA_AUTH_ENABLED=true \
  -e DAYTONA_AUTH_JWT_ALGORITHM=RS256 \
  -e DAYTONA_AUTH_JWT_PUBLIC_KEY="$(cat /etc/daytona/jwt.pub)" \
  -e DAYTONA_AUTH_JWT_ISSUER=daytona-runner \
  -e DAYTONA_AUTH_JWT_AUDIENCE=daytona-daemon \
  daytona-new:amd64
```

## 6. 验证方式

### 6.1 鉴权关闭时

```bash
curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls"}'
# 返回 200
```

### 6.2 鉴权开启时（无 token）

```bash
curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls"}'
# 返回 401，body: {"error":"unauthorized","reason":"missing id-token"}
```

### 6.3 鉴权开启时（有效 token）

```bash
TOKEN=$(./generate_jwt_token)  # 用相同 secret/algorithm 签发

curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -H "id-token: $TOKEN" \
  -d '{"command": "ls"}'
# 返回 200
```

### 6.4 鉴权开启时（过期 token）

```bash
curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -H "id-token: $EXPIRED_TOKEN" \
  -d '{"command": "ls"}'
# 返回 401，body: {"error":"unauthorized","reason":"token expired"}
```

## 7. 客户端签发 Token 示例（Python）

```python
import jwt, time

payload = {
    "iss": "daytona-runner",
    "aud": "daytona-daemon",
    "sub": "runner-55.40.15.144",
    "iat": int(time.time()),
    "exp": int(time.time()) + 3600,
    "scope": "process.execute fs.read",
}
token = jwt.encode(payload, "my-strong-shared-secret", algorithm="HS256")
print(token)
```

## 8. 与原方案的差异

| 项目 | 原方案（外部 URL 鉴权） | 新方案（本地 JWT 校验） |
|------|------------------------|------------------------|
| 依赖外部服务 | 必须 | 不需要 |
| 单次请求开销 | 1 次 HTTP 调用（数十毫秒） | 纯本地解析（< 1 毫秒） |
| 鉴权可用性 | 受外部服务可用性影响 | 仅受 daemon 自身可用性影响 |
| 鉴权状态信息 | URL 内部决定 | 7 种细分结果，定位更精确 |
| 配置复杂度 | URL + 超时 + 透传 | 算法 + 密钥 + 可选 iss/aud |
| 部署复杂度 | 需要先部署鉴权服务 | 开箱即用 |

## 9. 鉴权结果枚举

| 枚举值 | 含义 | HTTP 状态 |
|--------|------|-----------|
| `disabled` | 鉴权未启用 | - |
| `skipped` | 路径在白名单 | - |
| `passed` | 校验通过 | - |
| `missing_token` | 请求头中未带 id-token | 401 |
| `invalid_token` | JWT 格式错误或 base64 解析失败 | 401 |
| `invalid_signature` | 签名校验失败 | 401 |
| `expired` | token 已过期（exp） | 401 |
| `not_yet_valid` | token 尚未生效（nbf） | 401 |
| `invalid_issuer` | 签发方不匹配 | 401 |
| `invalid_audience` | 接收方不匹配 | 401 |
| `misconfigured` | 鉴权启用但未配置密钥/算法 | 500 |

## 10. 安全建议

1. **使用非对称算法（RS256/ES256）** 用于生产环境，避免共享密钥泄露风险。
2. **时钟同步**：确保 daemon 主机与 token 签发方使用 NTP 同步。
3. **定期轮换密钥**：HS256 场景下定期更新 `DAYTONA_AUTH_JWT_SECRET`。
4. **HTTPS 部署**：防止 token 在传输过程中被截获。
5. **白名单最小化**：仅放行真正不需要鉴权的接口（如健康检查）。
6. **日志脱敏**：Authorization/Cookie 等敏感头使用 base64 编码后再写入日志。

## 11. 文件变更清单

本章列明 `daemon-new` 相比原 `apps/daemon` 在源码上的所有改动。
下方表格中"状态"含义：

- **新增**：原本不存在，新创建的文件
- **修改**：原文件存在，已修改内容
- **删除**：原本存在，新版本已删除

### 11.1 新增文件

| 文件路径 | 行数（约） | 说明 |
|----------|------------|------|
| `pkg/toolbox/middlewares/auth_context.go` | 60 | 鉴权上下文结构 + 12 个 `AuthResult` 枚举值 |
| `pkg/toolbox/middlewares/jwt_verifier.go` | 200 | JWT 签名/声明校验器（支持 9 种算法） |
| `auth.yaml.example` | 110 | 本地 JWT 鉴权配置示例 |
| `docs/AUTH.md` | 270 | 本鉴权方案完整使用文档 |

### 11.2 修改文件

| 文件路径 | 变更内容 | 关键变化 |
|----------|----------|----------|
| `cmd/daemon/config/config.go` | `AuthConfig` 结构体 | 删除 `URL/TimeoutSec/PassthroughAuthorization`，新增 `IDTokenHeader/JWTAlgorithm/JWTSecret/JWTPublicKey/JWTIssuer/JWTAudience/ClockSkewSec`；默认值由鉴权 URL 改为 `id-token` 头 + HS256 + 30 秒时钟偏移 |
| `cmd/daemon/main.go` | Server 注入 | `toolBoxServer.AuthConfig = c` 注入到 toolbox.Server |
| `pkg/toolbox/toolbox.go` | 中间件注册 + import 别名 | 引入 `daemonconfig` 别名解决与 `toolbox/config` 的包冲突；将原来的 `ExternalAuthMiddleware` 替换为 `JWTAuthMiddleware`；`Server` 结构体新增 `AuthConfig *daemonconfig.Config` 字段 |
| `pkg/toolbox/middlewares/auth.go` | 完全重写 | 删除 `ExternalAuthMiddleware`、`verifyWithExternalAuth`、`buildAuthRequestBody` 三个函数，新增 `JWTAuthMiddleware` |
| `pkg/toolbox/middlewares/logging.go` | 增强 | 支持记录请求头（敏感头 base64 编码）、请求体（限大小）、鉴权结果 |
| `go.mod` | 依赖新增 | 添加 `github.com/golang-jwt/jwt/v5 v5.2.1` |
| `README.md` | 文档 | 顶部增加鉴权方案变更说明 + 链接到 AUTH.md |

### 11.3 删除文件

无。原 `apps/daemon` 的所有源文件均保留在 `daemon-new` 中，未删除任何代码。

### 11.4 关键接口与函数映射

| 原 `apps/daemon` 中的位置 | `daemon-new` 中的位置 | 说明 |
|---------------------------|------------------------|------|
| 无 | `middlewares/auth.go::JWTAuthMiddleware` | 新增：本地 JWT 校验中间件 |
| 无 | `middlewares/jwt_verifier.go::JWTVerifier` | 新增：JWT 解析与校验 |
| 无 | `middlewares/auth_context.go::AuthContext` | 新增：鉴权上下文（供日志读取） |
| `config/config.go::AuthConfig.URL` | `config/config.go::AuthConfig.JWTSecret` | 字段语义变更：URL→对称密钥 |
| `toolbox/toolbox.go::ExternalAuthMiddleware` | `toolbox/toolbox.go::JWTAuthMiddleware` | 整段中间件注册逻辑替换 |

### 11.5 回滚方案

如需回滚到原版（不启用鉴权），将 `DAYTONA_AUTH_ENABLED` 设为 `false` 即可，
daemon 启动后不注册 JWT 中间件，所有接口直接放行，行为与原版一致。

