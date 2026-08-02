// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package middlewares

import (
	"github.com/gin-gonic/gin"
)

// AuthResult 鉴权结果枚举
type AuthResult string

const (
	// AuthResultPassed 鉴权通过
	AuthResultPassed AuthResult = "passed"
	// AuthResultDisabled 鉴权未启用
	AuthResultDisabled AuthResult = "disabled"
	// AuthResultSkipped 白名单跳过
	AuthResultSkipped AuthResult = "skipped"
	// AuthResultMissingToken 请求头中缺少 id-token
	AuthResultMissingToken AuthResult = "missing_token"
	// AuthResultInvalidToken JWT 格式错误
	AuthResultInvalidToken AuthResult = "invalid_token"
	// AuthResultInvalidSignature 签名校验失败
	AuthResultInvalidSignature AuthResult = "invalid_signature"
	// AuthResultExpired token 已过期
	AuthResultExpired AuthResult = "expired"
	// AuthResultNotYetValid token 尚未生效
	AuthResultNotYetValid AuthResult = "not_yet_valid"
	// AuthResultInvalidIssuer 签发方不匹配
	AuthResultInvalidIssuer AuthResult = "invalid_issuer"
	// AuthResultInvalidAudience 接收方不匹配
	AuthResultInvalidAudience AuthResult = "invalid_audience"
	// AuthResultMisconfigured 鉴权配置错误（启用但缺少密钥/算法）
	AuthResultMisconfigured AuthResult = "misconfigured"
	// AuthResultRemoteCheckFailed 远程鉴权失败（二次校验 data=false 或非 2xx）
	AuthResultRemoteCheckFailed AuthResult = "remote_check_failed"
	// AuthResultRemoteCheckError 远程鉴权调用异常（超时/网络错误）
	AuthResultRemoteCheckError AuthResult = "remote_check_error"
)

// AuthContext 鉴权上下文（用于日志记录）
type AuthContext struct {
	Enabled bool       `json:"enabled"`
	Result  AuthResult `json:"result"`
	Reason  string     `json:"reason"`
	Latency int64      `json:"latency_ms"` // 鉴权耗时（毫秒）

	// 解析出的 JWT 声明（鉴权通过时填充）
	Subject  string `json:"subject,omitempty"`  // sub
	Issuer   string `json:"issuer,omitempty"`   // iss
	Audience string `json:"audience,omitempty"` // aud
}

const authContextKey = "daytona.auth.context"

// SetAuthContext 设置鉴权上下文到 gin.Context
func SetAuthContext(c *gin.Context, ctx *AuthContext) {
	c.Set(authContextKey, ctx)
}

// GetAuthContext 获取鉴权上下文
func GetAuthContext(c *gin.Context) (*AuthContext, bool) {
	v, ok := c.Get(authContextKey)
	if !ok {
		return nil, false
	}
	ctx, ok := v.(*AuthContext)
	return ctx, ok
}
