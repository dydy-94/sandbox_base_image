// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package middlewares

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	daemonconfig "github.com/daytonaio/daemon/cmd/daemon/config"
	log "github.com/sirupsen/logrus"
)

var ignoreLoggingPaths = map[string]bool{}

// LoggingMiddleware 访问日志中间件
//
// 通过 cfg.AccessLog 配置控制日志详细度：
//   - LogHeaders: 记录请求头
//   - LogBody: 记录请求体
//   - LogAuth: 记录鉴权拦截信息（仅在鉴权启用时生效）
//
// 注意：cfg 为 nil 时，行为与原版一致（仅记录 method/URI/status/latency）
func LoggingMiddleware(cfg *daemonconfig.Config) gin.HandlerFunc {
	return func(ctx *gin.Context) {
		startTime := time.Now()

		// 读取并缓存请求体（让后续 handler 仍能正常读取）
		var bodyBytes []byte
		if cfg != nil && cfg.AccessLog.LogBody && ctx.Request.Body != nil {
			maxSize := int64(cfg.AccessLog.BodyMaxSize)
			if maxSize <= 0 {
				maxSize = 4096
			}
			limitedReader := io.LimitReader(ctx.Request.Body, maxSize)
			bodyBytes, _ = io.ReadAll(limitedReader)
			// 还原 body 供后续 handler 使用
			ctx.Request.Body = io.NopCloser(bytes.NewBuffer(bodyBytes))
		}

		ctx.Next()
		endTime := time.Now()
		latencyTime := endTime.Sub(startTime)

		reqMethod := ctx.Request.Method
		reqUri := ctx.Request.RequestURI
		statusCode := ctx.Writer.Status()
		clientIP := ctx.ClientIP()

		// 是否需要排除该路径
		if cfg != nil && cfg.IsAccessLogExcluded(ctx.Request.URL.Path) {
			return
		}

		// 构造基础日志字段
		fields := log.Fields{
			"method":    reqMethod,
			"URI":       reqUri,
			"status":    statusCode,
			"latency":   latencyTime,
			"client_ip": clientIP,
		}

		// 记录鉴权相关上下文
		if cfg != nil && cfg.AccessLog.LogAuth {
			if authCtx, ok := GetAuthContext(ctx); ok {
				fields["auth_enabled"] = authCtx.Enabled
				fields["auth_result"] = authCtx.Result
				fields["auth_reason"] = authCtx.Reason
			}
		}

		// 记录请求头
		if cfg != nil && cfg.AccessLog.LogHeaders {
			headers := make(map[string]string)
			for k, v := range ctx.Request.Header {
				// 敏感头使用 base64 编码（可解码）便于后续问题定位
				if isSensitiveHeader(k) {
					headers[k] = "b64:" + base64Encode(strings.Join(v, ","))
				} else {
					headers[k] = strings.Join(v, ",")
				}
			}
			fields["headers"] = headers
		}

		// 记录请求体
		if cfg != nil && cfg.AccessLog.LogBody && len(bodyBytes) > 0 {
			bodyStr := string(bodyBytes)
			// 尝试格式化 JSON
			var prettyJSON bytes.Buffer
			if err := json.Indent(&prettyJSON, bodyBytes, "", "  "); err == nil {
				bodyStr = prettyJSON.String()
			}
			fields["body"] = bodyStr
		}

		// 实际输出日志
		if len(ctx.Errors) > 0 {
			fields["error"] = ctx.Errors.String()
			log.WithFields(fields).Error("API ERROR")
			return
		}

		fullPath := ctx.FullPath()
		if ignoreLoggingPaths[fullPath] {
			log.WithFields(fields).Debug("API REQUEST")
		} else {
			// 鉴权失败的请求使用 Warn 级别，便于告警
			if statusCode == cfg.GetAuthFailureStatus() {
				log.WithFields(fields).Warn("API REQUEST")
			} else {
				log.WithFields(fields).Info("API REQUEST")
			}
		}
	}
}

// isSensitiveHeader 判断是否为敏感请求头
func isSensitiveHeader(name string) bool {
	name = strings.ToLower(name)
	switch name {
	case "authorization", "cookie", "x-api-key", "x-auth-token":
		return true
	}
	return false
}

// base64Encode 对敏感头值进行 base64 编码（可解码），用于日志脱敏
//
// 日志中会显示为 "b64:xxxxxxxx"，运维人员需要时可以解码还原：
//   echo "xxxxxxxx" | base64 -d
//
// 这样既防止明文直接暴露，又保留了排查问题的能力。
func base64Encode(value string) string {
	return base64.StdEncoding.EncodeToString([]byte(value))
}
