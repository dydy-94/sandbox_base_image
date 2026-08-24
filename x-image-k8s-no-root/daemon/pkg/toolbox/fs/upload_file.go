// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package fs

import (
	"errors"
	"net/http"

	"github.com/daytonaio/daemon/internal/util"
	"github.com/daytonaio/daemon/pkg/toolbox/middlewares"
	"github.com/gin-gonic/gin"
	log "github.com/sirupsen/logrus"
)

// UploadFile godoc
//
//	@Summary		Upload a file
//	@Description	Upload a file to the specified path
//	@Tags			file-system
//	@Accept			multipart/form-data
//	@Param			path	query		string	true	"Destination path for the uploaded file"
//	@Param			file	formData	file	true	"File to upload"
//	@Success		200		{object}	gin.H
//	@Router			/files/upload [post]
//
//	@id				UploadFile
func UploadFile(c *gin.Context) {
	path := c.Query("path")
	if path == "" {
		c.AbortWithError(http.StatusBadRequest, errors.New("path is required"))
		return
	}

	file, err := c.FormFile("file")
	if err != nil {
		c.AbortWithError(http.StatusBadRequest, err)
		return
	}

	if err := c.SaveUploadedFile(file, path); err != nil {
		c.AbortWithError(http.StatusBadRequest, err)
		return
	}

	// multipart 请求体不会进访问日志的 body 字段（会破坏 handler 解析），
	// 所以文件名/大小在 handler 层补充记录，与访问日志字段风格保持一致。
	log.WithFields(log.Fields{
		"path":     util.SanitizeLogString(path),
		"filename": util.SanitizeLogString(file.Filename),
		"size":     file.Size,
	}).Info(middlewares.RequestLogTagCtx(c) + " files/upload ok")

	c.Status(http.StatusOK)
}
