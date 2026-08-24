// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package middlewares

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	daemonconfig "github.com/daytonaio/daemon/cmd/daemon/config"
	"github.com/daytonaio/daemon/internal/util"
	log "github.com/sirupsen/logrus"
)

var ignoreLoggingPaths = map[string]bool{}

// responseLogMax 响应体日志最大字节数（防止大结果污染日志，超长截断）
const responseLogMax = 4096

// captureWriter 包装 gin.ResponseWriter，捕获响应体用于日志，超长自动截断
// （只保留前 responseLogMax 字节，后续写入直接透传不再缓存，避免大结果全量缓冲）
type captureWriter struct {
	gin.ResponseWriter
	body      []byte
	truncated bool
}

func (w *captureWriter) Write(b []byte) (int, error) {
	if len(w.body) < responseLogMax {
		remain := responseLogMax - len(w.body)
		if len(b) <= remain {
			w.body = append(w.body, b...)
		} else {
			w.body = append(w.body, b[:remain]...)
			w.truncated = true
		}
	} else {
		w.truncated = true
	}
	return w.ResponseWriter.Write(b)
}

func (w *captureWriter) WriteString(s string) (int, error) {
	return w.Write([]byte(s))
}

// loadBashrcVars 从 ~/.bashrc 实时读取日志标记所需的沙箱变量
// （不做缓存：daemon 预启动后运行期下发的变量必须能被读到）
func loadBashrcVars() map[string]string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = "/home/x"
	}
	values := map[string]string{}
	fillBashrc(values, filepath.Join(home, ".bashrc"))
	return values
}

// RequestLogTag 构造日志标记前缀（全部取自已下发到 ~/.bashrc 的沙箱变量），
// 供访问日志与各业务 handler 日志共用：
//
//	[X_SANDBOX_USER_ID][X_SANDBOX_USER_NAME][X_SANDBOX_TYPE][X_SANDBOX_ID]
func RequestLogTag() string {
	v := loadBashrcVars()
	return "[" + v["X_SANDBOX_USER_ID"] + "][" + v["X_SANDBOX_USER_NAME"] +
		"][" + v["X_SANDBOX_TYPE"] + "][" + v["X_SANDBOX_ID"] + "]"
}

// requestTraceIDKey 是 x-b3-traceid 存入 gin.Context 的键
const requestTraceIDKey = "trace_id"

// SetTraceID 从请求头 x-b3-traceid 提取链路追踪 ID 并存入 gin.Context，
// 供 RequestLogTagCtx 拼进日志前缀。请求头缺失时不做任何事。
func SetTraceID(c *gin.Context) {
	if c == nil {
		return
	}
	if tid := c.GetHeader("X-B3-TraceId"); tid != "" {
		c.Set(requestTraceIDKey, tid)
	}
}

// RequestLogTagCtx 与 RequestLogTag 相同，但在最前面带上当前请求的
// x-b3-traceid（链路追踪 ID），形如 [traceid][uid][uname][type][id]。
// 请求头没有 trace id 时保持原前缀不变，不影响旧日志格式。
func RequestLogTagCtx(c *gin.Context) string {
	if c == nil {
		return RequestLogTag()
	}
	if traceID := c.GetString(requestTraceIDKey); traceID != "" {
		return "[" + traceID + "]" + RequestLogTag()
	}
	return RequestLogTag()
}

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

		// 提取链路追踪 ID（X-B3-TraceId）供日志前缀使用
		SetTraceID(ctx)

		// 读取并缓存请求体（让后续 handler 仍能正常读取）。
		// 注意：必须读完整 body 再还原，日志侧才截断——早期实现只读前
		// maxSize 字节就还原，长命令（>4KB）的 JSON 被切断导致后续
		// ShouldBindJSON 失败、误报 "command is required"。
		var bodyBytes []byte
		var bodyTruncated bool
		if cfg != nil && cfg.AccessLog.LogBody && ctx.Request.Body != nil &&
			shouldCaptureRequestBody(ctx.Request.Header.Get("Content-Type")) {
			maxSize := int64(cfg.AccessLog.BodyMaxSize)
			if maxSize <= 0 {
				maxSize = 4096
			}
			fullBody, _ := io.ReadAll(ctx.Request.Body)
			// 还原完整 body 供后续 handler 使用（不得截断）
			ctx.Request.Body = io.NopCloser(bytes.NewBuffer(fullBody))
			// 日志侧只保留前 maxSize 字节，超长部分丢弃并标记
			bodyBytes = fullBody
			if int64(len(fullBody)) > maxSize {
				bodyBytes = fullBody[:maxSize]
				bodyTruncated = true
			}
		}

		// 包裹 ResponseWriter 捕获响应体（用于结果日志，超长自动截断）。
		// 文件内容类接口（/files/*）不捕获——响应就是文件内容，既不写日志也不缓存进内存
		var capture *captureWriter
		if !isFileContentPath(ctx.Request.URL.Path) {
			capture = &captureWriter{ResponseWriter: ctx.Writer}
			ctx.Writer = capture
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

		// 记录 GET 类请求的 query 参数（重要入参）
		if rawQuery := ctx.Request.URL.RawQuery; rawQuery != "" {
			fields["query"] = util.SanitizeLogString(rawQuery)
		}

		// 记录响应结果（超长自动截断，并标记截断）。
		// 文件内容类接口（/files/*）capture 为 nil，天然不记录
		if capture != nil && len(capture.body) > 0 {
			fields["response"] = util.SanitizeLogString(string(capture.body))
			if capture.truncated {
				fields["response_truncated"] = true
			}
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
			fields["body"] = util.SanitizeLogString(bodyStr)
			if bodyTruncated {
				fields["body_truncated"] = true
			}
		}

		// 日志标记前缀：【traceId】【sapId】【name】【type】【id】
		// （traceId 取自请求头 X-B3-TraceId，其余从 ~/.bashrc 实时取）
		tag := RequestLogTagCtx(ctx)

		// 实际输出日志
		if len(ctx.Errors) > 0 {
			fields["error"] = ctx.Errors.String()
			log.WithFields(fields).Error(tag + " API ERROR")
			return
		}

		fullPath := ctx.FullPath()
		if ignoreLoggingPaths[fullPath] {
			log.WithFields(fields).Debug(tag + " API REQUEST")
		} else {
			// 鉴权失败的请求使用 Warn 级别，便于告警
			if statusCode == cfg.GetAuthFailureStatus() {
				log.WithFields(fields).Warn(tag + " API REQUEST")
			} else {
				log.WithFields(fields).Info(tag + " API REQUEST")
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

// isFileContentPath 判断接口是否可能返回文件内容。
//
// /files/* 下的读文件、下载等接口，响应体就是文件内容（文本或二进制），
// 打印到日志会泄漏文件数据，统一跳过 response 字段。请求体侧已由
// shouldCaptureRequestBody 排除 multipart / octet-stream。
func isFileContentPath(path string) bool {
	return strings.HasPrefix(path, "/files")
}

// shouldCaptureRequestBody 判断请求体能否安全捕获用于日志。
//
// multipart/form-data（文件上传）与原始二进制流如果被 LimitReader 截断后
// 再还原回 ctx.Request.Body，handler 解析 multipart 必然失败（body 不完整），
// 所以这类请求体跳过捕获、不做任何改动；仅捕获可安全读取的 JSON/文本类。
func shouldCaptureRequestBody(contentType string) bool {
	switch {
	case strings.Contains(contentType, "multipart/"):
		return false
	case strings.Contains(contentType, "application/octet-stream"):
		return false
	case strings.Contains(contentType, "application/x-www-form-urlencoded"):
		return false
	default:
		return true
	}
}

// base64Encode 对敏感头值进行 base64 编码（可解码），用于日志脱敏
//
// 日志中会显示为 "b64:xxxxxxxx"，运维人员需要时可以解码还原：
//
//	echo "xxxxxxxx" | base64 -d
//
// 这样既防止明文直接暴露，又保留了排查问题的能力。
func base64Encode(value string) string {
	return base64.StdEncoding.EncodeToString([]byte(value))
}
