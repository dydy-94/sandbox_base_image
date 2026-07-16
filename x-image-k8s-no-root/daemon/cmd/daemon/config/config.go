// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package config

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/go-playground/validator/v10"
	"github.com/kelseyhightower/envconfig"
	"gopkg.in/yaml.v3"

	log "github.com/sirupsen/logrus"
)

type Config struct {
	DaemonLogFilePath            string `envconfig:"DAYTONA_DAEMON_LOG_FILE_PATH"`
	EntrypointLogFilePath        string `envconfig:"DAYTONA_ENTRYPOINT_LOG_FILE_PATH"`
	EntrypointShutdownTimeoutSec int    `envconfig:"ENTRYPOINT_SHUTDOWN_TIMEOUT_SEC"`
	SigtermShutdownTimeoutSec    int    `envconfig:"SIGTERM_SHUTDOWN_TIMEOUT_SEC"`
	UserHomeAsWorkDir            bool   `envconfig:"DAYTONA_USER_HOME_AS_WORKDIR"`

	// 鉴权相关
	Auth AuthConfig `yaml:"auth"`

	// 访问日志相关
	AccessLog AccessLogConfig `yaml:"access_log"`
}

// AccessLogConfig 访问日志详细度配置
type AccessLogConfig struct {
	// 是否记录请求头
	// 环境变量: DAYTONA_ACCESS_LOG_HEADERS
	LogHeaders bool `yaml:"log_headers" envconfig:"DAYTONA_ACCESS_LOG_HEADERS"`

	// 是否记录请求体（仅记录 JSON）
	// 环境变量: DAYTONA_ACCESS_LOG_BODY
	LogBody bool `yaml:"log_body" envconfig:"DAYTONA_ACCESS_LOG_BODY"`

	// 是否记录鉴权拦截（鉴权失败/鉴权跳过）
	// 环境变量: DAYTONA_ACCESS_LOG_AUTH
	LogAuth bool `yaml:"log_auth" envconfig:"DAYTONA_ACCESS_LOG_AUTH"`

	// 记录请求体的最大字节数（防止大请求体污染日志）
	// 环境变量: DAYTONA_ACCESS_LOG_BODY_MAX_SIZE
	BodyMaxSize int `yaml:"body_max_size" envconfig:"DAYTONA_ACCESS_LOG_BODY_MAX_SIZE"`

	// 不记录访问日志的路径
	// 环境变量: DAYTONA_ACCESS_LOG_EXCLUDE_PATHS（多个用逗号分隔）
	ExcludePaths []string `yaml:"exclude_paths" envconfig:"DAYTONA_ACCESS_LOG_EXCLUDE_PATHS"`
}

// AuthConfig 本地 JWT 鉴权配置
//
// 校验行为按环境变量启用，默认全部关闭（仅做格式解析与声明提取）：
//   - DAYTONA_AUTH_VALIDATE_SIGNATURE=true   校验签名（必须配置 jwt_secret 或 jwt_public_key）
//   - DAYTONA_AUTH_VALIDATE_EXPIRATION=true  校验 exp / nbf 声明
//   - DAYTONA_AUTH_VALIDATE_ISSUER=true      校验 iss 声明（必须配置 jwt_issuer）
//   - DAYTONA_AUTH_VALIDATE_AUDIENCE=true    校验 aud 声明（必须配置 jwt_audience）
type AuthConfig struct {
	// 是否启用鉴权
	// 环境变量: DAYTONA_AUTH_ENABLED
	Enabled bool `yaml:"enabled" envconfig:"DAYTONA_AUTH_ENABLED"`

	// 携带 token 的请求头名
	// 环境变量: DAYTONA_AUTH_ID_TOKEN_HEADER（默认 id-token）
	IDTokenHeader string `yaml:"id_token_header" envconfig:"DAYTONA_AUTH_ID_TOKEN_HEADER"`

	// 是否校验 JWT 签名（默认 false）
	// 启用后必须配置 jwt_secret（HS*）或 jwt_public_key（RS*/ES*）
	// 环境变量: DAYTONA_AUTH_VALIDATE_SIGNATURE
	ValidateSignature bool `yaml:"validate_signature" envconfig:"DAYTONA_AUTH_VALIDATE_SIGNATURE"`

	// 是否校验 token 有效期（exp / nbf，默认 false）
	// 环境变量: DAYTONA_AUTH_VALIDATE_EXPIRATION
	ValidateExpiration bool `yaml:"validate_expiration" envconfig:"DAYTONA_AUTH_VALIDATE_EXPIRATION"`

	// 是否校验 iss 声明（默认 false）
	// 启用后必须配置 jwt_issuer
	// 环境变量: DAYTONA_AUTH_VALIDATE_ISSUER
	ValidateIssuer bool `yaml:"validate_issuer" envconfig:"DAYTONA_AUTH_VALIDATE_ISSUER"`

	// 是否校验 aud 声明（默认 false）
	// 启用后必须配置 jwt_audience
	// 环境变量: DAYTONA_AUTH_VALIDATE_AUDIENCE
	ValidateAudience bool `yaml:"validate_audience" envconfig:"DAYTONA_AUTH_VALIDATE_AUDIENCE"`

	// JWT 签名算法（仅在 validate_signature=true 时使用）
	// 支持：HS256/HS384/HS512/RS256/RS384/RS512/ES256/ES384/ES512
	// 环境变量: DAYTONA_AUTH_JWT_ALGORITHM（默认 HS256）
	JWTAlgorithm string `yaml:"jwt_algorithm" envconfig:"DAYTONA_AUTH_JWT_ALGORITHM"`

	// 对称算法（HS*）使用的共享密钥（仅在 validate_signature=true 时使用）
	// 环境变量: DAYTONA_AUTH_JWT_SECRET
	JWTSecret string `yaml:"jwt_secret" envconfig:"DAYTONA_AUTH_JWT_SECRET"`

	// 非对称算法（RS*/ES*）使用的公钥（仅在 validate_signature=true 时使用）
	// 支持 PEM 内容或 "@/path/to/key.pem" 形式的文件路径
	// 环境变量: DAYTONA_AUTH_JWT_PUBLIC_KEY
	JWTPublicKey string `yaml:"jwt_public_key" envconfig:"DAYTONA_AUTH_JWT_PUBLIC_KEY"`

	// 期望的 iss 声明（仅在 validate_issuer=true 时使用，留空视为启用但跳过校验）
	// 环境变量: DAYTONA_AUTH_JWT_ISSUER
	JWTIssuer string `yaml:"jwt_issuer" envconfig:"DAYTONA_AUTH_JWT_ISSUER"`

	// 期望的 aud 声明（仅在 validate_audience=true 时使用，留空视为启用但跳过校验）
	// 环境变量: DAYTONA_AUTH_JWT_AUDIENCE
	JWTAudience string `yaml:"jwt_audience" envconfig:"DAYTONA_AUTH_JWT_AUDIENCE"`

	// 时钟偏移容忍（秒，仅在 validate_expiration=true 时使用）
	// 环境变量: DAYTONA_AUTH_CLOCK_SKEW_SEC（默认 30）
	ClockSkewSec int `yaml:"clock_skew_sec" envconfig:"DAYTONA_AUTH_CLOCK_SKEW_SEC"`

	// 不需要鉴权的路径白名单
	// 环境变量: DAYTONA_AUTH_EXCLUDE_PATHS（多个用逗号分隔）
	ExcludePaths []string `yaml:"exclude_paths" envconfig:"DAYTONA_AUTH_EXCLUDE_PATHS"`

	// 鉴权失败时返回的状态码
	// 环境变量: DAYTONA_AUTH_FAILURE_STATUS（默认 401）
	FailureStatus int `yaml:"failure_status" envconfig:"DAYTONA_AUTH_FAILURE_STATUS"`

	// 是否启用远程鉴权二次校验
	// JWT 本地校验通过后，再调一次外部接口确认（防止 token 合法但 sandbox 已下架等场景）
	// 环境变量: SANDBOX_AUTH_CHECK（默认 false）
	SandboxAuthCheck bool `yaml:"sandbox_auth_check" envconfig:"SANDBOX_AUTH_CHECK"`

	// 远程鉴权 URL
	// 环境变量: SANDBOX_AUTH_CHECK_URL
	SandboxAuthCheckURL string `yaml:"sandbox_auth_check_url" envconfig:"SANDBOX_AUTH_CHECK_URL"`

	// 远程鉴权超时（秒，默认 3）
	// 环境变量: SANDBOX_AUTH_CHECK_TIMEOUT_SEC
	SandboxAuthCheckTimeoutSec int `yaml:"sandbox_auth_check_timeout_sec" envconfig:"SANDBOX_AUTH_CHECK_TIMEOUT_SEC"`

	// 远程鉴权请求体模板（JSON 字符串）
	// 支持 ${VAR_NAME} 占位符，从 os.Getenv("VAR_NAME") 取值替换
	// 默认: {"sandboxName":"${HOSTNAME}"}
	// 环境变量: SANDBOX_AUTH_CHECK_BODY
	SandboxAuthCheckBody string `yaml:"sandbox_auth_check_body" envconfig:"SANDBOX_AUTH_CHECK_BODY"`
}

var defaultDaemonLogFilePath = "/tmp/daytona-daemon.log"
var defaultEntrypointLogFilePath = "/tmp/daytona-entrypoint.log"

// 鉴权配置文件的默认查找路径
var defaultAuthConfigPaths = []string{
	"$HOME/.daemon/auth.yaml",
	"/home/x/.daemon/auth.yaml",
	"/etc/daytona/auth.yaml",
}

var config *Config

func GetConfig() (*Config, error) {
	if config != nil {
		return config, nil
	}

	config = &Config{}

	// 1. 先尝试加载鉴权配置文件（优先级最高）
	loadAuthConfigFromFile(config)

	// 2. 用环境变量覆盖（环境变量优先级高于配置文件）
	err := envconfig.Process("", config)
	if err != nil {
		log.Error(err)
		os.Exit(2)
	}

	var validate = validator.New()
	err = validate.Struct(config)
	if err != nil {
		return nil, err
	}

	if config.DaemonLogFilePath == "" {
		config.DaemonLogFilePath = defaultDaemonLogFilePath
	}

	if config.EntrypointLogFilePath == "" {
		config.EntrypointLogFilePath = defaultEntrypointLogFilePath
	}

	if config.EntrypointShutdownTimeoutSec <= 0 {
		// Default to 10 seconds
		config.EntrypointShutdownTimeoutSec = 10
	}

	if config.SigtermShutdownTimeoutSec <= 0 {
		// Default to 5 seconds
		config.SigtermShutdownTimeoutSec = 5
	}

	// 鉴权配置默认值
	if config.Auth.IDTokenHeader == "" {
		config.Auth.IDTokenHeader = "id-token"
	}
	if config.Auth.JWTAlgorithm == "" {
		config.Auth.JWTAlgorithm = "HS256"
	}
	if config.Auth.ClockSkewSec <= 0 {
		config.Auth.ClockSkewSec = 30
	}
	if config.Auth.FailureStatus <= 0 {
		config.Auth.FailureStatus = 401
	}
	if config.Auth.SandboxAuthCheckTimeoutSec <= 0 {
		config.Auth.SandboxAuthCheckTimeoutSec = 3
	}
	if config.Auth.SandboxAuthCheckBody == "" {
		// 默认 body 模板：取 HOSTNAME 环境变量（K8s 里是 pod name）
		config.Auth.SandboxAuthCheckBody = `{"sandboxName":"${HOSTNAME}"}`
	}
	if len(config.Auth.ExcludePaths) == 0 {
		config.Auth.ExcludePaths = []string{
			"/version",
			"/health",
			"/user-home-dir",
			"/work-dir",
		}
	}

	// 访问日志默认值
	if config.AccessLog.BodyMaxSize <= 0 {
		config.AccessLog.BodyMaxSize = 4096 // 默认最大 4KB
	}
	if len(config.AccessLog.ExcludePaths) == 0 {
		config.AccessLog.ExcludePaths = []string{
			"/version",
			"/health",
		}
	}

	log.Infof("Auth enabled: %v, algorithm: %s, header: %s, ExcludePaths: %v",
		config.Auth.Enabled, config.Auth.JWTAlgorithm,
		config.Auth.IDTokenHeader, config.Auth.ExcludePaths)
	log.Infof("AccessLog: headers=%v, body=%v, auth=%v, body_max_size=%d",
		config.AccessLog.LogHeaders, config.AccessLog.LogBody,
		config.AccessLog.LogAuth, config.AccessLog.BodyMaxSize)

	return config, nil
}

// loadAuthConfigFromFile 从配置文件中加载鉴权配置
// 查找路径顺序：$HOME/.daemon/auth.yaml -> /home/x/.daemon/auth.yaml -> /etc/daytona/auth.yaml
func loadAuthConfigFromFile(cfg *Config) {
	homeDir, _ := os.UserHomeDir()

	for _, p := range defaultAuthConfigPaths {
		path := os.ExpandEnv(p)
		if path == "" {
			continue
		}
		// 如果是相对路径，基于 homeDir 解析
		if !filepath.IsAbs(path) && homeDir != "" {
			path = filepath.Join(homeDir, path)
		}

		if _, err := os.Stat(path); err == nil {
			log.Infof("Loading auth config from: %s", path)
			if data, err := os.ReadFile(path); err == nil {
				if err := yaml.Unmarshal(data, cfg); err != nil {
					log.Warnf("Failed to parse auth config file %s: %v", path, err)
				}
			} else {
				log.Warnf("Failed to read auth config file %s: %v", path, err)
			}
			return
		}
	}

	log.Info("No auth config file found, using environment variables or defaults")
}

// IsPathExcluded 判断路径是否在鉴权白名单中
func (c *Config) IsPathExcluded(path string) bool {
	for _, p := range c.Auth.ExcludePaths {
		if strings.EqualFold(path, p) {
			return true
		}
	}
	return false
}

// IsAccessLogExcluded 判断路径是否在访问日志排除列表中
func (c *Config) IsAccessLogExcluded(path string) bool {
	for _, p := range c.AccessLog.ExcludePaths {
		if strings.EqualFold(path, p) {
			return true
		}
	}
	return false
}

// GetAuthFailureStatus 获取鉴权失败状态码（带默认值）
func (c *Config) GetAuthFailureStatus() int {
	if c.Auth.FailureStatus <= 0 {
		return 401
	}
	return c.Auth.FailureStatus
}
