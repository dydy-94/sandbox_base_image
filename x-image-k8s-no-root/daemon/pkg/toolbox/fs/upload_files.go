// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package fs

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/daytonaio/daemon/internal/util"
	"github.com/daytonaio/daemon/pkg/toolbox/middlewares"
	"github.com/gin-gonic/gin"
	log "github.com/sirupsen/logrus"
)

// UploadFiles godoc
//
//	@Summary		Upload multiple files
//	@Description	Upload multiple files with their destination paths
//	@Tags			file-system
//	@Accept			multipart/form-data
//	@Success		200
//	@Router			/files/bulk-upload [post]
//
//	@id				UploadFiles
func UploadFiles(c *gin.Context) {
	reader, err := c.Request.MultipartReader()
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"errors": []string{"invalid multipart form"}})
		return
	}

	dests := make(map[string]string)
	var errs []string
	// 收集每个 part 的元信息用于日志（multipart 请求体不会进访问日志 body 字段）
	type uploadMeta struct {
		field    string
		filename string
		dest     string
	}
	var metas []uploadMeta

	for {
		part, err := reader.NextPart()
		if err == io.EOF {
			break
		}
		if err != nil {
			errs = append(errs, fmt.Sprintf("reading part: %v", err))
			continue
		}

		name := part.FormName()

		if strings.HasSuffix(name, ".path") {
			data, err := io.ReadAll(part)
			if err != nil {
				idx := extractIndex(name)
				errs = append(errs, fmt.Sprintf("path[%s]: %v", idx, err))
				continue
			}
			idx := extractIndex(name)
			dests[idx] = string(data)
			metas = append(metas, uploadMeta{field: name, dest: string(data)})
			continue
		}

		if strings.HasSuffix(name, ".file") {
			idx := extractIndex(name)
			dest, ok := dests[idx]
			if !ok {
				errs = append(errs, fmt.Sprintf("file[%s]: missing .path metadata", idx))
				continue
			}

			if d := filepath.Dir(dest); d != "" {
				if err := os.MkdirAll(d, 0o755); err != nil {
					errs = append(errs, fmt.Sprintf("%s: mkdir %s: %v", dest, d, err))
					continue
				}
			}

			f, err := os.Create(dest)
			if err != nil {
				errs = append(errs, fmt.Sprintf("%s: create: %v", dest, err))
				continue
			}

			if _, err := io.Copy(f, part); err != nil {
				errs = append(errs, fmt.Sprintf("%s: write: %v", dest, err))
			}
			f.Close()
			metas = append(metas, uploadMeta{field: name, filename: part.FileName(), dest: dest})
			continue
		}
	}

	// 日志输出（顺序稳定：按 part 字段名排序）
	if len(metas) > 0 {
		sort.Slice(metas, func(i, j int) bool { return metas[i].field < metas[j].field })
		files := make([]string, 0, len(metas))
		for _, m := range metas {
			if m.filename != "" {
				files = append(files, fmt.Sprintf("%s→%s", m.filename, m.dest))
			} else {
				files = append(files, fmt.Sprintf("path(%s)=%s", m.field, m.dest))
			}
		}
		log.WithFields(log.Fields{
			"files": util.SanitizeLogString(strings.Join(files, "; ")),
			"count": len(files),
		}).Info(middlewares.RequestLogTagCtx(c) + " files/bulk-upload ok")
	}

	if len(errs) > 0 {
		c.JSON(http.StatusBadRequest, gin.H{"errors": errs})
		return
	}

	c.Status(http.StatusOK)
}

func extractIndex(fieldName string) string {
	s := strings.TrimPrefix(fieldName, "files[")
	return strings.TrimSuffix(strings.TrimSuffix(s, "].path"), "].file")
}
