// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

// Package middlewares 提供 HTTP 鉴权中间件。
//
// 本地 JWT 鉴权中间件：
//   - 从请求头中读取 id-token（可配置 header 名）
//   - 按需校验：仅在对应环境变量启用时才校验签名/exp/nbf/iss/aud
//   - 全部校验项关闭时，仅做 base64 + JSON 解析，把 claims 透传给业务层
//   - 鉴权通过后放行，否则返回配置的失败状态码
//
// 关闭鉴权（DAYTONA_AUTH_ENABLED=false）时，本中间件等同于无操作。
package middlewares

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/golang-jwt/jwt/v5"

	daemonconfig "github.com/daytonaio/daemon/cmd/daemon/config"
	log "github.com/sirupsen/logrus"
)

// JWTAuthMiddleware 本地 JWT 鉴权中间件
//
// 工作模式：
//   - cfg.Auth.Enabled = false：直接放行，记录 auth_result=disabled
//   - cfg.Auth.Enabled = true：
//   - 白名单路径：直接放行，记录 auth_result=skipped
//   - 缺少 id-token：返回 401，记录 auth_result=missing_token
//   - token 格式/签名/有效期/iss/aud 任一校验失败：返回 401，记录对应的细分结果
//   - 全部通过：放行，记录 auth_result=passed
func JWTAuthMiddleware(cfg *daemonconfig.Config) (gin.HandlerFunc, error) {
	// 鉴权关闭时直接返回空操作
	if !cfg.Auth.Enabled {
		return func(c *gin.Context) {
			SetAuthContext(c, &AuthContext{
				Enabled: false,
				Result:  AuthResultDisabled,
				Reason:  "auth disabled by configuration",
			})
			c.Next()
		}, nil
	}

	verifier, err := NewJWTVerifier(cfg.Auth)
	if err != nil {
		log.Errorf("Failed to create JWT verifier: %v", err)
		// 启动失败，应该阻止 daemon 启动
		return nil, err
	}

	headerName := cfg.Auth.IDTokenHeader
	if headerName == "" {
		headerName = "id-token"
	}

	log.Infof("JWT auth enabled: algorithm=%s, header=%s, issuer=%q, audience=%q",
		cfg.Auth.JWTAlgorithm, headerName, cfg.Auth.JWTIssuer, cfg.Auth.JWTAudience)

	return func(c *gin.Context) {
		authStart := time.Now()
		authCtx := &AuthContext{Enabled: true}

		defer func() {
			authCtx.Latency = time.Since(authStart).Milliseconds()
			SetAuthContext(c, authCtx)
		}()

		// 白名单路径直接放行
		if isPathExcluded(c.Request.URL.Path, cfg.Auth.ExcludePaths) {
			authCtx.Result = AuthResultSkipped
			authCtx.Reason = "path in exclude list"
			c.Next()
			return
		}

		// 读取 token
		tokenString := c.GetHeader(headerName)
		if tokenString == "" {
			// 兼容大小写：尝试常见变体
			tokenString = c.GetHeader(strings.ToLower(headerName))
		}
		if tokenString == "" {
			authCtx.Result = AuthResultMissingToken
			authCtx.Reason = "missing " + headerName + " header"
			abortWithAuthError(c, cfg.Auth.FailureStatus, authCtx)
			return
		}

		// 校验 token
		claims, err := verifier.Verify(tokenString)
		if err != nil {
			authCtx.Result = mapVerifyError(err)
			authCtx.Reason = err.Error()
			abortWithAuthError(c, cfg.Auth.FailureStatus, authCtx)
			return
		}

		// 提取声明用于日志
		if sub, ok := claims["sub"].(string); ok {
			authCtx.Subject = sub
		}
		if iss, ok := claims["iss"].(string); ok {
			authCtx.Issuer = iss
		}
		if aud, ok := claims["aud"].(string); ok {
			authCtx.Audience = aud
		}

		authCtx.Result = AuthResultPassed
		authCtx.Reason = "jwt verification passed"

		// 远程二次鉴权（JWT 本地校验通过后调用，SANDBOX_AUTH_CHECK=true 时启用）
		if cfg.Auth.SandboxAuthCheck {
			remoteOK, remoteReason := performRemoteSandboxCheck(c.Request.Context(), cfg.Auth, tokenString)
			if !remoteOK {
				authCtx.Result = mapRemoteCheckResult(remoteReason)
				authCtx.Reason = remoteReason
				abortWithAuthError(c, cfg.Auth.FailureStatus, authCtx)
				return
			}
			authCtx.Result = AuthResultPassed
			authCtx.Reason = "jwt passed + remote sandbox check passed"
		}

		c.Next()
	}, nil
}

// remoteCheckResponse 远程鉴权接口的 JSON 响应结构
type remoteCheckResponse struct {
	Success bool   `json:"success"`
	Code    int    `json:"code"`
	Message string `json:"message"`
	Data    bool   `json:"data"`
}

// envVarPattern 匹配 body 模板里的 ${VAR_NAME} 占位符
var envVarPattern = regexp.MustCompile(`\$\{([A-Z_][A-Z0-9_]*)\}`)

// renderBodyTemplate 把 body 模板里的 ${VAR_NAME} 替换成 os.Getenv("VAR_NAME")
// 未设置的环境变量保留为 ${VAR_NAME} 原样（方便排错）
func renderBodyTemplate(tpl string) string {
	return envVarPattern.ReplaceAllStringFunc(tpl, func(m string) string {
		groups := envVarPattern.FindStringSubmatch(m)
		if len(groups) < 2 {
			return m
		}
		if v := os.Getenv(groups[1]); v != "" {
			return v
		}
		return m
	})
}

// performRemoteSandboxCheck 调远程接口做 sandbox 二次鉴权
//
// 请求格式：POST <SANDBOX_AUTH_CHECK_URL>
// header:
//   - Content-Type: application/json
//   - id-token: 透传进来的 token
// body: 由 SANDBOX_AUTH_CHECK_BODY 模板渲染后得到，${VAR_NAME} 占位符从环境变量替换
//
// 判定规则：HTTP 2xx 且响应 JSON 的 data == true 才算通过
// 失败时返回 (false, remoteMessage) — remoteMessage 优先取响应里的 message 字段
func performRemoteSandboxCheck(ctx context.Context, cfg daemonconfig.AuthConfig, tokenString string) (bool, string) {
	if cfg.SandboxAuthCheckURL == "" {
		return false, "SANDBOX_AUTH_CHECK enabled but SANDBOX_AUTH_CHECK_URL is empty"
	}

	// 渲染 body 模板
	bodyTemplate := cfg.SandboxAuthCheckBody
	if bodyTemplate == "" {
		bodyTemplate = `{"sandboxName":"${HOSTNAME}"}`
	}
	renderedBody := renderBodyTemplate(bodyTemplate)

	timeout := time.Duration(cfg.SandboxAuthCheckTimeoutSec) * time.Second
	if timeout <= 0 {
		timeout = 3 * time.Second
	}

	reqCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodPost, cfg.SandboxAuthCheckURL, bytes.NewReader([]byte(renderedBody)))
	if err != nil {
		return false, fmt.Sprintf("build remote check request: %v", err)
	}
	req.Header.Set("Content-Type", "application/json")
	// 透传 id-token
	headerName := cfg.IDTokenHeader
	if headerName == "" {
		headerName = "id-token"
	}
	req.Header.Set(headerName, tokenString)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return false, fmt.Sprintf("remote sandbox check http error: %v", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return false, fmt.Sprintf("remote sandbox check http status %d", resp.StatusCode)
	}

	var body remoteCheckResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return false, fmt.Sprintf("decode remote sandbox check response: %v", err)
	}

	if !body.Data {
		// data != true 时把远程 message 字段透传出去
		msg := body.Message
		if msg == "" {
			msg = fmt.Sprintf("remote sandbox check denied: success=%v code=%d data=%v",
				body.Success, body.Code, body.Data)
		}
		return false, msg
	}

	return true, ""
}

// mapRemoteCheckResult 把远程鉴权失败的 reason 映射到 AuthResult 枚举
func mapRemoteCheckResult(reason string) AuthResult {
	if strings.HasPrefix(reason, "remote sandbox check http error") ||
		strings.HasPrefix(reason, "build remote check request") ||
		strings.HasPrefix(reason, "decode remote sandbox check response") ||
		strings.HasPrefix(reason, "SANDBOX_AUTH_CHECK enabled but") ||
		strings.HasPrefix(reason, "invalid SANDBOX_AUTH_CHECK_URL") {
		return AuthResultRemoteCheckError
	}
	return AuthResultRemoteCheckFailed
}

// isPathExcluded 判断路径是否在白名单中
func isPathExcluded(path string, paths []string) bool {
	for _, p := range paths {
		if strings.EqualFold(path, p) {
			return true
		}
	}
	return false
}

// mapVerifyError 将 Verify 返回的 error 映射到 AuthResult 枚举
func mapVerifyError(err error) AuthResult {
	switch {
	case errors.Is(err, ErrMissingToken):
		return AuthResultMissingToken
	case errors.Is(err, ErrInvalidToken):
		return AuthResultInvalidToken
	case errors.Is(err, ErrInvalidSignature):
		return AuthResultInvalidSignature
	case errors.Is(err, ErrTokenExpired):
		return AuthResultExpired
	case errors.Is(err, ErrTokenNotYetValid):
		return AuthResultNotYetValid
	case errors.Is(err, ErrInvalidIssuer):
		return AuthResultInvalidIssuer
	case errors.Is(err, ErrInvalidAudience):
		return AuthResultInvalidAudience
	case errors.Is(err, ErrMisconfigured):
		return AuthResultMisconfigured
	default:
		// 兜底：可能是 jwt 库的原始错误
		if errors.Is(err, jwt.ErrTokenExpired) {
			return AuthResultExpired
		}
		if errors.Is(err, jwt.ErrTokenNotValidYet) {
			return AuthResultNotYetValid
		}
		if errors.Is(err, jwt.ErrTokenSignatureInvalid) {
			return AuthResultInvalidSignature
		}
		if errors.Is(err, jwt.ErrTokenMalformed) {
			return AuthResultInvalidToken
		}
		return AuthResultInvalidToken
	}
}

// abortWithAuthError 中断请求并返回鉴权错误响应
func abortWithAuthError(c *gin.Context, statusCode int, authCtx *AuthContext) {
	if statusCode <= 0 {
		statusCode = 401
	}
	log.Warnf("Auth failed: result=%s, reason=%s, ip=%s, path=%s",
		authCtx.Result, authCtx.Reason, c.ClientIP(), c.Request.URL.Path)
	c.AbortWithStatusJSON(statusCode, gin.H{
		"error":  "unauthorized",
		"reason": authCtx.Reason,
	})
}
