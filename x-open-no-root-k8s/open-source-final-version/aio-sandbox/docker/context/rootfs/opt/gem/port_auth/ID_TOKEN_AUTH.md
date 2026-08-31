# id-token 鉴权接入规范（供其他服务改造复用）

> 目的：让沙箱内其他服务（非 daytona daemon）用**同一套算法与逻辑**校验 `id-token`：
> ① 校验 token 有效性（RS256 签名 + exp）；② 校验 `sap_id` 是否在允许通过的列表中。
>
> 参考实现：本目录 `verifier.py`（token 有效性）、`policy.py`（业务规则）、`trusted_jwks.json`（公钥）。
{
  "keys": [
    {
      "kty": "RSA",
      "e": "AQAB",
      "kid": "rsa1",
      "alg": "RS256",
      "n": "qt6yOiI_wCoCVlGO0MySsez0VkSqhPvDl3rfabOslx35mYEO-n4ABfIT5Gn2zN-CeIcOZ5ugAXvIIRWv5H55-tzjFazi5IKkOIMCiz5__MtsdxKCqGlZu2zt-BLpqTOAPiflNPpM3RUAlxKAhnYEqNha6-allPnFQupnW_eTYoyuzuedT7dSp90ry0ZcQDimntXWeaSbrYKCj9Rr9W1jn2uTowUuXaScKXTCjAmJVnsD75JNzQfa8DweklTyWQF-Y5Ky039I0VIu-0CIGhXY48GAFe2EFb8VpNhf07DP63p138RWQ1d3KPEM9mYJVpQC68j3wzDQYSljpLf9by7TGw"
    }
  ]
}

---

## 1. token 形态

- 位置：**请求头 `Id-Token`**（大小写不敏感，nginx 变量 `$http_id_token`）
- 类型：**JWT**，签名算法固定 **RS256**
- Header 必须携带 **`kid`**，取值必须命中受信 JWKS 中的 key

## 2. 判定总流程（按序执行，任一失败即拒绝）

```
1. 沙箱上下文不可用，或 X_SANDBOX_TYPE != "USER"
   └── 放行（非用户沙箱 / 系统内部流量，不校验）
2. 请求头缺失 Id-Token
   └── 401 missing_token
3. 解析 JWT header
   ├── 格式错误            → 401 malformed_token
   ├── alg != "RS256"      → 401 unsupported_algorithm
   └── 无 kid / kid 不在 JWKS → 401 missing_kid / unknown_kid
4. 用 kid 对应的公钥验签
   └── 失败                → 401 invalid_signature
5. 校验 exp
   ├── 无 exp / exp 非数字 → 401 invalid_exp
   └── now >= exp          → 401 expired_token
6. 校验 sub
   └── sub 以 @native 结尾  → 放行（系统原生 token）
7. 校验 sap_id（允许列表）
   ├── 取 sap_id；为空兜底 rtc_id
   │    ├── 两者皆空        → 403 missing_user_claim
   │    └── 取到 user_id     ↓
   ├── user_id 不在允许列表  → 403 user_not_allowed
   └── user_id 在允许列表    → 放行（204）
```

> 建议：`@native` 豁免与「允许列表」可拆成独立配置，按需开关。

## 3. 签名校验算法（token 有效性）

```python
import jwt

ALGORITHM = "RS256"
# 公钥来源：trusted_jwks.json（沙箱内预置，轮换密钥只改这个文件）
keys = {k["kid"]: k for k in json.loads(Path("trusted_jwks.json").read_text())["keys"]}

def verify_token(token: str) -> dict:
    header = jwt.get_unverified_header(token)          # 1. 解析 header
    if header.get("alg") != ALGORITHM:                 # 2. 算法必须是 RS256
        raise PermissionError("unsupported_algorithm")
    kid = header.get("kid")                            # 3. kid 必须命中受信 key
    if not kid or kid not in keys:
        raise PermissionError("unknown_kid")
    payload = jwt.decode(
        token,
        keys[kid],                                     # 4. 公钥验签
        algorithms=[ALGORITHM],
        options={"require": ["exp"], "verify_exp": False, "verify_aud": False},
    )
    return payload
```

**要点（与 reference 逐条对齐，防绕过）：**

| 项 | 要求 | 防止的绕过 |
|---|---|---|
| `alg` | 必须 = RS256（白名单） | alg 混淆攻击（改成 HS256 用公钥当密钥签） |
| `kid` | 必须命中受信 JWKS | 伪造 kid 指向任意公钥 |
| 验签 | 用 kid 对应公钥 | 篡改 payload/header |
| `exp` | 必须存在，且 `now < exp` | 永久 token |
| `aud` / `iss` | 本方案不校验（按需扩展） | - |

**exp 判定**（verifier 关闭 `verify_exp` 后自行比对，便于给不同 reason）：

```python
import math, time
exp = payload.get("exp")
if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(float(exp)):
    raise PermissionError("invalid_exp")
if time.time() >= float(exp):
    raise PermissionError("expired_token")
```

## 4. sap_id 允许列表校验（业务规则）

```python
def check_sap_id(payload: dict, allowed: set[str]) -> None:
    user_id = str(payload.get("sap_id") or "").strip()
    if not user_id:                                    # 兜底 rtc_id
        user_id = str(payload.get("rtc_id") or "").strip()
    if not user_id:
        raise PermissionError("missing_user_claim")
    if user_id.strip().lower() not in {s.strip().lower() for s in allowed}:
        raise PermissionError("user_not_allowed")      # 403
    # 放行
```

**允许列表来源建议（三选一，配置化）：**
1. 环境变量：`ALLOWED_SAP_IDS=a,b,c`
2. 文件：`/opt/gem/port_auth/allowed_sap_ids.txt`（每行一个，`#` 注释）
3. 接口：启动时拉取，定期刷新（带超时与降级策略）

比对统一 **strip + lower**，避免大小写/空白误拒。

## 5. 沙箱上下文（第 1 步的判定依据）

- 来源优先级：`/home/x/.daemon/runtime/env/service_env.json` → `/home/x/.bashrc` → 进程环境变量
- 需要键：`X_SANDBOX_TYPE`、`X_SANDBOX_USER_ID`
- 判定：`X_SANDBOX_TYPE` 缺失或不为 `USER` → 放行（非用户沙箱不鉴权）

> 若你的服务**永远只处理用户沙箱流量**，可跳过第 1 步，直接走 token 校验。

## 6. 接入方式（选一）

### A. nginx auth_request 复用（推荐，服务零改造）
服务挂在端口反代后面（`<port>-<name>` 子域），nginx 已 `auth_request` 调 port_auth，**业务代码完全不用碰 token**。新服务只需遵循同一 nginx 模板。

### B. 调用 port_auth 本地接口
服务启动后自带鉴权逻辑时，可同步调：
```
GET/HEAD/POST ... http://127.0.0.1:18081/auth
Header: Id-Token: <token>
```
- 204 → 放行；401/403 → 拒绝（reason 见第 7 节）；503 → 上下文未就绪

### C. 代码内复刻
把第 3、4 节逻辑直接搬进服务（Go/Python/Node 都有 JWT 库）。失败时返回对应状态码与 reason。

## 7. 状态码与 reason 枚举（与 port_auth 保持一致）

| 状态码 | reason | 含义 |
|---|---|---|
| 204 | allow | 放行 |
| 401 | missing_token | 缺 Id-Token 头 |
| 401 | token_too_large | token 超过 32KB |
| 401 | malformed_token | JWT 格式错误 |
| 401 | unsupported_algorithm | alg 非 RS256 |
| 401 | missing_kid | header 无 kid |
| 401 | unknown_kid | kid 不在受信 JWKS |
| 401 | invalid_signature | 验签失败 |
| 401 | invalid_exp | exp 缺失/非数字 |
| 401 | expired_token | token 已过期 |
| 403 | missing_user_claim | sap_id/rtc_id 皆无 |
| 403 | user_not_allowed | sap_id 不在允许列表 |
| 403 | user_mismatch | sap_id 与沙箱用户不匹配（旧规则，已被允许列表取代时可去掉） |
| 503 | service_env_invalid / context 未就绪 | 沙箱上下文加载失败 |

## 8. 改造 checklist（对齐参考实现）

- [ ] 读 `Id-Token` 头（不是 `Authorization: Bearer`）
- [ ] JWKS 公钥引用同一份 `trusted_jwks.json`（或同步同内容）
- [ ] `alg` 白名单 = `["RS256"]`，且 `kid` 必须命中
- [ ] 验签后自行校验 `exp`（`now >= exp` 拒绝）
- [ ] `sub` 以 `@native` 结尾放行（可选）
- [ ] `sap_id`（兜底 `rtc_id`）strip+lower 后查允许列表
- [ ] 失败响应带细分 reason（方便日志排查）
- [ ] 密钥轮换只更新 JWKS，服务不重启配置

## 9. 测试用例

| 场景 | 构造 | 期望 |
|---|---|---|
| 无 token | 不发头 | 401 missing_token |
| 篡改 payload | 改 claims 后原签名 | 401 invalid_signature |
| 过期 | exp = 过去时间 | 401 expired_token |
| 无 exp | 删 exp 字段 | 401 invalid_exp |
| alg=HS256 | 改 header alg | 401 unsupported_algorithm |
| sap_id 不在列表 | sap_id=other | 403 user_not_allowed |
| sap_id 大小写/空格 | " abc123 " | 放行（strip+lower） |
| sap_id 缺失、rtc_id 在列表 | 只带 rtc_id | 放行 |
| sub=xxx@native | 任意签名合法 token | 放行 |
