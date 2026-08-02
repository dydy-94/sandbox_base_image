// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package middlewares

import (
	"crypto/ecdsa"
	"crypto/rsa"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/golang-jwt/jwt/v5"

	daemonconfig "github.com/daytonaio/daemon/cmd/daemon/config"
)

// JWT 校验返回的细分错误
var (
	ErrMissingToken     = errors.New("missing token")
	ErrInvalidToken     = errors.New("invalid token format")
	ErrInvalidSignature = errors.New("invalid signature")
	ErrTokenExpired     = errors.New("token expired")
	ErrTokenNotYetValid = errors.New("token not yet valid")
	ErrInvalidIssuer    = errors.New("invalid issuer")
	ErrInvalidAudience  = errors.New("invalid audience")
	ErrMisconfigured    = errors.New("auth misconfigured")
)

// JWTVerifier 本地 JWT 校验器
//
// 校验项由外部配置控制（见 AuthConfig 的 Validate* 开关），按需开启。
// 全部开关均关闭时，校验器只做 base64 解码与 JSON 解析，把 claims 原文返回。
type JWTVerifier struct {
	cfg daemonconfig.AuthConfig

	// 缓存解析后的密钥（仅在 validate_signature=true 时被加载）
	secret    []byte
	publicKey interface{}
}

// NewJWTVerifier 构造 JWT 校验器
//
// 仅当 AuthConfig.ValidateSignature=true 时才读取 jwt_secret / jwt_public_key，
// 因此未启用签名校验的场景不会触发文件 IO。
func NewJWTVerifier(cfg daemonconfig.AuthConfig) (*JWTVerifier, error) {
	v := &JWTVerifier{cfg: cfg}

	if cfg.ValidateSignature {
		algo := strings.ToUpper(strings.TrimSpace(cfg.JWTAlgorithm))
		if algo == "" {
			algo = "HS256"
			v.cfg.JWTAlgorithm = algo
		}
		if err := v.loadKey(); err != nil {
			return nil, err
		}
	}

	return v, nil
}

// loadKey 按算法加载签名密钥
func (v *JWTVerifier) loadKey() error {
	algo := v.cfg.JWTAlgorithm
	switch algo {
	case "HS256", "HS384", "HS512":
		secret := v.cfg.JWTSecret
		if secret == "" {
			return fmt.Errorf("%w: jwt_secret is required for %s", ErrMisconfigured, algo)
		}
		v.secret = []byte(secret)
		return nil

	case "RS256", "RS384", "RS512", "PS256", "PS384", "PS512":
		pub, err := loadRSAPublicKey(v.cfg.JWTPublicKey)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrMisconfigured, err)
		}
		v.publicKey = pub
		return nil

	case "ES256", "ES384", "ES512":
		pub, err := loadECDSAPublicKey(v.cfg.JWTPublicKey)
		if err != nil {
			return fmt.Errorf("%w: %v", ErrMisconfigured, err)
		}
		v.publicKey = pub
		return nil

	default:
		return fmt.Errorf("%w: unsupported algorithm %q", ErrMisconfigured, algo)
	}
}

// loadRSAPublicKey 解析 RSA 公钥，支持 PEM 内容或 "@/path" 文件路径
func loadRSAPublicKey(src string) (*rsa.PublicKey, error) {
	pemBytes, err := readKeySource(src)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("failed to decode PEM block for RSA public key")
	}
	pub, err := jwt.ParseRSAPublicKeyFromPEM(pemBytes)
	if err != nil {
		return nil, fmt.Errorf("parse RSA public key: %w", err)
	}
	_ = block
	return pub, nil
}

// loadECDSAPublicKey 解析 ECDSA 公钥
func loadECDSAPublicKey(src string) (*ecdsa.PublicKey, error) {
	pemBytes, err := readKeySource(src)
	if err != nil {
		return nil, err
	}
	pub, err := jwt.ParseECPublicKeyFromPEM(pemBytes)
	if err != nil {
		return nil, fmt.Errorf("parse ECDSA public key: %w", err)
	}
	return pub, nil
}

// readKeySource 支持直接 PEM 内容或 "@/path/to/file" 文件引用
func readKeySource(src string) ([]byte, error) {
	if src == "" {
		return nil, errors.New("public key source is empty")
	}
	if strings.HasPrefix(src, "@") {
		path := strings.TrimPrefix(src, "@")
		return os.ReadFile(path)
	}
	return []byte(src), nil
}

// Verify 解析并按配置校验 JWT
//
// 返回 (claims, error)：
//   - error == nil：校验通过（或未启用相关校验），claims 已填充
//   - error != nil：校验失败，对应细分错误（Err*）
func (v *JWTVerifier) Verify(tokenString string) (jwt.MapClaims, error) {
	if tokenString == "" {
		return nil, ErrMissingToken
	}

	parserOpts := []jwt.ParserOption{
		// 不限制 alg：未启用签名校验时也要正常解析出 claims
		jwt.WithoutClaimsValidation(),
	}
	// 仅在启用有效期校验时设置时钟偏移
	if v.cfg.ValidateExpiration {
		leeway := time.Duration(v.cfg.ClockSkewSec) * time.Second
		if leeway < 0 {
			leeway = 0
		}
		// 取消 WithoutClaimsValidation 并开启时间校验
		parserOpts = []jwt.ParserOption{
			jwt.WithLeeway(leeway),
		}
		// jwt/v5 的 exp/nbf 在解析阶段自动校验，前提是不加 WithoutClaimsValidation
	}

	parser := jwt.NewParser(parserOpts...)

	var (
		token *jwt.Token
		err   error
	)

	if v.cfg.ValidateSignature {
		// 启用签名校验：必须根据算法挑选 keyFunc
		keyFunc, err := v.keyFunc()
		if err != nil {
			return nil, err
		}
		parserOpts = append(parserOpts, jwt.WithValidMethods([]string{v.cfg.JWTAlgorithm}))
		parser = jwt.NewParser(parserOpts...)
		token, err = parser.Parse(tokenString, keyFunc)
	} else {
		// 未启用签名校验：跳过 keyFunc，仅做格式 / 声明解析
		token, _, err = parser.ParseUnverified(tokenString, jwt.MapClaims{})
	}

	if err != nil {
		return nil, mapParseError(err)
	}

	if token == nil {
		return nil, ErrInvalidToken
	}

	claims, ok := token.Claims.(jwt.MapClaims)
	if !ok {
		return nil, ErrInvalidToken
	}

	// 显式触发 exp / nbf 校验（仅在启用时）
	if v.cfg.ValidateExpiration {
		now := time.Now()
		leeway := time.Duration(v.cfg.ClockSkewSec) * time.Second

		if exp, ok := claims["exp"].(float64); ok {
			if now.After(time.Unix(int64(exp), 0).Add(leeway)) {
				return nil, ErrTokenExpired
			}
		} else if v.cfg.ValidateExpiration {
			// exp 缺失：仅在明确要求 exp 时报错；否则跳过
			// 这里采用宽松策略：缺失不报错
		}

		if nbf, ok := claims["nbf"].(float64); ok {
			if now.Add(leeway).Before(time.Unix(int64(nbf), 0)) {
				return nil, ErrTokenNotYetValid
			}
		}
	}

	// 显式触发 iss 校验
	if v.cfg.ValidateIssuer {
		iss, _ := claims["iss"].(string)
		if v.cfg.JWTIssuer != "" && iss != v.cfg.JWTIssuer {
			return nil, ErrInvalidIssuer
		}
	}

	// 显式触发 aud 校验
	if v.cfg.ValidateAudience {
		if v.cfg.JWTAudience != "" && !validateAudience(claims["aud"], v.cfg.JWTAudience) {
			return nil, ErrInvalidAudience
		}
	}

	return claims, nil
}

// keyFunc 根据算法返回对应的签名密钥
func (v *JWTVerifier) keyFunc() (jwt.Keyfunc, error) {
	switch v.cfg.JWTAlgorithm {
	case "HS256", "HS384", "HS512":
		return func(t *jwt.Token) (interface{}, error) {
			if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
				return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
			}
			return v.secret, nil
		}, nil
	case "RS256", "RS384", "RS512", "PS256", "PS384", "PS512":
		return func(t *jwt.Token) (interface{}, error) {
			if _, ok := t.Method.(*jwt.SigningMethodRSA); !ok {
				return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
			}
			return v.publicKey, nil
		}, nil
	case "ES256", "ES384", "ES512":
		return func(t *jwt.Token) (interface{}, error) {
			if _, ok := t.Method.(*jwt.SigningMethodECDSA); !ok {
				return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
			}
			return v.publicKey, nil
		}, nil
	default:
		return nil, fmt.Errorf("%w: unsupported algorithm %q", ErrMisconfigured, v.cfg.JWTAlgorithm)
	}
}

// mapParseError 把 jwt 库的内部错误转换为对外的细分错误
func mapParseError(err error) error {
	switch {
	case errors.Is(err, jwt.ErrTokenExpired):
		return ErrTokenExpired
	case errors.Is(err, jwt.ErrTokenNotValidYet):
		return ErrTokenNotYetValid
	case errors.Is(err, jwt.ErrTokenSignatureInvalid):
		return ErrInvalidSignature
	case errors.Is(err, jwt.ErrTokenMalformed):
		return ErrInvalidToken
	default:
		return fmt.Errorf("token validation failed: %w", err)
	}
}

// validateAudience 校验 aud 声明，支持单值或数组
func validateAudience(aud interface{}, expected string) bool {
	switch v := aud.(type) {
	case string:
		return v == expected
	case []interface{}:
		for _, item := range v {
			if s, ok := item.(string); ok && s == expected {
				return true
			}
		}
	case []string:
		for _, s := range v {
			if s == expected {
				return true
			}
		}
	}
	return false
}
