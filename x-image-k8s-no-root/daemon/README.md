# Daytona Daemon 鉴权改造使用文档

> **鉴权方案**：本地 JWT 校验（取消外部 URL 鉴权接口）。
> 完整说明请参考 [docs/AUTH.md](docs/AUTH.md)。

## 1. 项目结构

```
daemon-new/
├── auth.yaml.example               # 鉴权配置示例
├── Dockerfile                       # Docker 构建文件
├── docs/
│   └── AUTH.md                      # 鉴权详细说明
├── cmd/daemon/
│   ├── main.go                      # 主入口（已修改：注入 AuthConfig）
│   └── config/
│       └── config.go                # 配置加载（已添加 AuthConfig）
├── pkg/toolbox/
│   ├── toolbox.go                   # HTTP 服务（已修改：注册鉴权中间件）
│   └── middlewares/
│       ├── auth.go                  # 鉴权中间件（JWT 本地校验）
│       ├── jwt_verifier.go          # JWT 签名与声明校验器
│       ├── auth_context.go          # 鉴权上下文（用于日志）
│       └── logging.go               # 访问日志（带 base64 脱敏）
└── ...
```

## 2. 鉴权配置文件

### 2.1 文件位置

按以下顺序查找（找到第一个存在的即用）：

1. `$HOME/.daemon/auth.yaml`
2. `/home/x/.daemon/auth.yaml`
3. `/etc/daytona/auth.yaml`

### 2.2 文件示例（参考 `auth.yaml.example`）

```yaml
# 鉴权开关（环境变量: DAYTONA_AUTH_ENABLED）
enabled: false

# 鉴权接口 URL
url: "http://127.0.0.1:9999/verify"

# 超时秒数
timeout_sec: 5

# 是否透传 Authorization 头
passthrough_authorization: true

# 白名单
exclude_paths:
  - /version
  - /health
  - /user-home-dir
  - /work-dir

# 失败状态码
failure_status: 401
```

## 3. 环境变量配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `DAYTONA_AUTH_ENABLED` | `false` | 是否启用鉴权 |
| `DAYTONA_AUTH_URL` | - | 鉴权接口 URL |
| `DAYTONA_AUTH_TIMEOUT_SEC` | `5` | 鉴权超时（秒） |
| `DAYTONA_AUTH_PASSTHROUGH_AUTH_HEADER` | `false` | 是否透传 Authorization 头 |
| `DAYTONA_AUTH_EXCLUDE_PATHS` | - | 白名单（逗号分隔） |
| `DAYTONA_AUTH_FAILURE_STATUS` | `401` | 鉴权失败状态码 |

**优先级**：环境变量 > 配置文件

## 4. 鉴权接口协议

### 4.1 请求方式

daemon 收到 API 请求后会向 `DAYTONA_AUTH_URL` 发起 `POST` 请求。

### 4.2 请求体（JSON）

```json
{
  "method": "POST",
  "path": "/process/execute",
  "query": "command=ls",
  "body": { ... }  // 原始请求体（如果是 JSON）
}
```

### 4.3 请求头

- `Content-Type: application/json`
- `Authorization: <客户端原值>`（如果启用 `passthrough_authorization`）
- `X-Forwarded-For: <客户端 IP>`

### 4.4 鉴权接口响应

- 成功（2xx）：放行 daemon 原始请求
- 失败（非 2xx）：daemon 返回 `401`（或 `failure_status` 配置值）

## 5. 部署方式

### 5.1 鉴权关闭（默认）

```bash
# 容器启动后默认鉴权关闭（DAYTONA_AUTH_ENABLED=false）
docker run -d --name daytona-new -p 8080:8080 daytona-new:amd64
```

### 5.2 鉴权开启（通过环境变量）

```bash
docker run -d --name daytona-new -p 8080:8080 \
  -e DAYTONA_AUTH_ENABLED=true \
  -e DAYTONA_AUTH_URL=http://auth-service:9999/verify \
  daytona-new:amd64
```

### 5.3 鉴权开启（通过配置文件）

```bash
# 1. 准备配置文件
mkdir -p /home/x/.daemon
cat > /home/x/.daemon/auth.yaml <<EOF
auth:
  enabled: true
  url: "http://auth-service:9999/verify"
  timeout_sec: 3
  exclude_paths:
    - /version
    - /health
EOF

# 2. 挂载配置文件启动
docker run -d --name daytona-new -p 8080:8080 \
  -v /home/x/.daemon/auth.yaml:/home/x/.daemon/auth.yaml:ro \
  daytona-new:amd64
```

## 6. 构建镜像

```bash
cd /Users/daidai/daytona_cmb/daemon-new

# 构建 amd64 镜像
docker buildx build --platform linux/amd64 -t daytona-new:amd64 --load .

# 保存为 tar 文件
docker save -o daytona-new-amd64.tar daytona-new:amd64
```

## 7. 验证

### 7.1 验证鉴权关闭

```bash
curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls"}'
# 应该返回 200
```

### 7.2 验证鉴权开启

```bash
# 鉴权开启后，未授权请求应返回 401
curl -X POST http://localhost:8080/process/execute \
  -H "Content-Type: application/json" \
  -d '{"command": "ls"}'
# 应该返回 401
```

## 8. 鉴权服务示例（参考实现）

```python
# auth_server.py
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/verify', methods=['POST'])
def verify():
    data = request.get_json()
    auth_header = request.headers.get('Authorization', '')
    
    # 简单鉴权逻辑：检查 token
    if auth_header != 'Bearer valid-token':
        return jsonify({'error': 'unauthorized'}), 401
    
    # 鉴权通过
    return jsonify({'status': 'ok'}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9999)
```
