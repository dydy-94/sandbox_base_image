// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package internal

var (
	Version = "v0.0.0-dev"
	// BuildTime 与 GitCommit 由编译期 ldflags 注入（-X .../internal.BuildTime=...），
	// 用于启动日志与 /version 接口确认部署镜像是否为期望版本。
	BuildTime = "unknown"
	GitCommit = "unknown"
)
