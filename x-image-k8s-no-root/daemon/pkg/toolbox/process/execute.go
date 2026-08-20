// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package process

import (
	"bytes"
	"errors"
	"net/http"
	"os/exec"
	"time"

	log "github.com/sirupsen/logrus"

	"github.com/gin-gonic/gin"

	"github.com/daytonaio/daemon/internal/util"
	"github.com/daytonaio/daemon/pkg/toolbox/middlewares"
)

// ExecuteCommand godoc
//
//	@Summary		Execute a command
//	@Description	Execute a shell command and return the output and exit code
//	@Tags			process
//	@Accept			json
//	@Produce		json
//	@Param			request	body		ExecuteRequest	true	"Command execution request"
//	@Success		200		{object}	ExecuteResponse
//	@Router			/process/execute [post]
//
//	@id				ExecuteCommand
func ExecuteCommand(c *gin.Context) {
	start := time.Now()
	var (
		exitCode  int  = -1
		ok        bool = false // 是否成功返回 200（区别于业务退出码 0）
		timedOut  bool
		badReqMsg string // 参数错误原因
	)

	// 用 defer 在函数返回前统一打日志，覆盖所有 return 路径
	defer func() {
		fields := log.Fields{
			"duration_ms": time.Since(start).Milliseconds(),
			"exit_code":   exitCode,
			"ok":          ok,
		}
		if timedOut {
			fields["timed_out"] = true
		}
		if badReqMsg != "" {
			fields["bad_request"] = badReqMsg
		}

		switch {
		case badReqMsg != "":
			// 400 参数错误：Warn
			log.WithFields(fields).Warn(middlewares.RequestLogTag() + " execute command rejected")
		case timedOut:
			// 超时 408：Error 级别，便于告警
			log.WithFields(fields).Error(middlewares.RequestLogTag() + " execute command timeout")
		case !ok:
			// 其他返回路径（Aborted、handler panic 等）：Error
			log.WithFields(fields).Error(middlewares.RequestLogTag() + " execute command failed")
		case exitCode != 0:
			// 业务退出码非 0：Warn，便于运维扫一眼
			log.WithFields(fields).Warn(middlewares.RequestLogTag() + " execute command exited non-zero")
		default:
			// 完全成功：Info
			log.WithFields(fields).Info(middlewares.RequestLogTag() + " execute command ok")
		}
	}()

	var request ExecuteRequest
	if err := c.ShouldBindJSON(&request); err != nil {
		badReqMsg = "command is required"
		c.AbortWithError(http.StatusBadRequest, errors.New(badReqMsg))
		return
	}

	cmdParts := parseCommand(request.Command)
	if len(cmdParts) == 0 {
		badReqMsg = "empty command"
		c.AbortWithError(http.StatusBadRequest, errors.New(badReqMsg))
		return
	}

	// 进入执行前打一条 Info，便于排查"调用了但未返回"的请求。
	// request.Command 可能携带 ANSI 彩色码（例如安装脚本里的高亮 URL），
	// 打日志前统一剥离，避免日志出现 [36m/[0m 乱码。
	log.Infof("%s execute command start: %q timeout=%s",
		middlewares.RequestLogTag(), util.SanitizeLogString(request.Command), formatTimeout(request.Timeout))

	cmd := exec.Command(cmdParts[0], cmdParts[1:]...)
	if request.Cwd != nil {
		cmd.Dir = *request.Cwd
	}

	// set maximum execution time
	timeout := 360 * time.Second
	if request.Timeout != nil && *request.Timeout > 0 {
		timeout = time.Duration(*request.Timeout) * time.Second
	}

	timeoutReached := false
	timer := time.AfterFunc(timeout, func() {
		timeoutReached = true
		if cmd.Process != nil {
			// kill the process group
			err := cmd.Process.Kill()
			if err != nil {
				log.Error(err)
				return
			}
		}
	})
	defer timer.Stop()

	output, err := cmd.CombinedOutput()
	if err != nil {
		if timeoutReached {
			timedOut = true
			c.AbortWithError(http.StatusRequestTimeout, errors.New("command execution timeout"))
			return
		}
		if exitError, exitErrOk := err.(*exec.ExitError); exitErrOk {
			exitCode = exitError.ExitCode()
			ok = true
			c.JSON(http.StatusOK, ExecuteResponse{
				ExitCode: exitCode,
				Result:   string(output),
			})
			return
		}
		ok = true
		c.JSON(http.StatusOK, ExecuteResponse{
			ExitCode: -1,
			Result:   string(output),
		})
		return
	}

	if cmd.ProcessState == nil {
		ok = true
		c.JSON(http.StatusOK, ExecuteResponse{
			ExitCode: -1,
			Result:   string(output),
		})
		return
	}

	exitCode = cmd.ProcessState.ExitCode()
	ok = true
	c.JSON(http.StatusOK, ExecuteResponse{
		ExitCode: exitCode,
		Result:   string(output),
	})
}

// formatTimeout 把请求里的可选 timeout 格式化为可读字符串
func formatTimeout(t *uint32) string {
	if t == nil {
		return "default(360s)"
	}
	return (time.Duration(*t) * time.Second).String()
}

// parseCommand splits a command string properly handling quotes
func parseCommand(command string) []string {
	var args []string
	var current bytes.Buffer
	var inQuotes bool
	var quoteChar rune

	for _, r := range command {
		switch {
		case r == '"' || r == '\'':
			if !inQuotes {
				inQuotes = true
				quoteChar = r
			} else if quoteChar == r {
				inQuotes = false
				quoteChar = 0
			} else {
				current.WriteRune(r)
			}
		case r == ' ' && !inQuotes:
			if current.Len() > 0 {
				args = append(args, current.String())
				current.Reset()
			}
		default:
			current.WriteRune(r)
		}
	}

	if current.Len() > 0 {
		args = append(args, current.String())
	}

	return args
}
