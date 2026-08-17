// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package middlewares

// 沙箱上下文加载器（对齐 sandbox port_auth 的 environment.py）：
//
// 读取 X_SANDBOX_TYPE / X_SANDBOX_USER_ID / X_SANDBOX_USER_NAME，来源优先级：
//   1. /home/x/.daemon/runtime/env/service_env.json
//   2. ~/.bashrc （静态 export 行，拒绝含 $ ` \ 的引用值）
//   3. 进程环境变量
//
// 与 port_auth 不同：这里每次请求实时读取，不做永久缓存——daemon 是镜像
// 里预启动的，运行期才下发的环境变量（写 ~/.bashrc 或 export）必须能被读到。

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

// SandboxContext 沙箱上下文
type SandboxContext struct {
	SandboxType string
	UserID      string
	UserName    string
}

const sandboxUserType = "USER"

var (
	bashrcAssignmentRe = regexp.MustCompile(`^\s*(?:export\s+)?(X_SANDBOX_TYPE|X_SANDBOX_USER_ID|X_SANDBOX_USER_NAME|X_SANDBOX_ID)\s*=\s*(.*?)\s*$`)
	unsafeQuotedChars  = "$`\\"
	unsafeBareChars    = " ;$`|&()<>\t"
)

// LoadSandboxContext 实时加载沙箱上下文（每次请求调用，不做缓存）。
// ok=false 表示上下文缺失或不完整（沙箱类型未知，或 USER 类型缺 user id）。
func LoadSandboxContext() (SandboxContext, bool) {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		home = "/home/x"
	}

	values := map[string]string{}
	fillServiceEnv(values, filepath.Join(home, ".daemon", "runtime", "env", "service_env.json"))
	fillBashrc(values, filepath.Join(home, ".bashrc"))
	for _, key := range []string{"X_SANDBOX_TYPE", "X_SANDBOX_USER_ID", "X_SANDBOX_USER_NAME"} {
		if values[key] == "" {
			values[key] = strings.TrimSpace(os.Getenv(key))
		}
	}

	sandboxType := strings.ToUpper(strings.TrimSpace(values["X_SANDBOX_TYPE"]))
	userID := strings.TrimSpace(values["X_SANDBOX_USER_ID"])
	if sandboxType == "" || (sandboxType == sandboxUserType && userID == "") {
		return SandboxContext{}, false
	}
	return SandboxContext{
		SandboxType: sandboxType,
		UserID:      userID,
		UserName:    strings.TrimSpace(values["X_SANDBOX_USER_NAME"]),
	}, true
}

func fillServiceEnv(values map[string]string, path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var payload map[string]any
	if err := json.Unmarshal(data, &payload); err != nil {
		return
	}
	for _, key := range []string{"X_SANDBOX_TYPE", "X_SANDBOX_USER_ID", "X_SANDBOX_USER_NAME"} {
		if values[key] != "" {
			continue
		}
		if v, ok := payload[key]; ok {
			values[key] = claimText(v)
		}
	}
}

func fillBashrc(values map[string]string, path string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return
	}
	for _, line := range strings.Split(string(data), "\n") {
		m := bashrcAssignmentRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		if values[m[1]] != "" {
			continue
		}
		values[m[1]] = parseStaticShellValue(m[2])
	}
}

// parseStaticShellValue 解析静态 shell 赋值值：
//   - 单引号或双引号包裹：取内值；双引号内含 $ ` \ 视为不安全返回空
//   - 裸值：含 ; $ ` | & ( ) < > 空格 tab 视为不安全返回空
func parseStaticShellValue(raw string) string {
	value := strings.TrimSpace(raw)
	if value == "" {
		return ""
	}
	if len(value) >= 2 && value[0] == value[len(value)-1] && (value[0] == '\'' || value[0] == '"') {
		inner := value[1 : len(value)-1]
		if value[0] == '"' && strings.ContainsAny(inner, unsafeQuotedChars) {
			return ""
		}
		return inner
	}
	if strings.ContainsAny(value, unsafeBareChars) {
		return ""
	}
	return value
}

// claimText 把 JSON/claims 里的值转为规范化字符串（对齐 port_auth _claim_text）
func claimText(value any) string {
	switch v := value.(type) {
	case nil, bool, map[string]any, []any:
		return ""
	case string:
		return strings.TrimSpace(v)
	case json.Number:
		return strings.TrimSpace(v.String())
	case float64:
		return strings.TrimSpace(fmt.Sprintf("%v", v))
	default:
		return strings.TrimSpace(fmt.Sprint(v))
	}
}
