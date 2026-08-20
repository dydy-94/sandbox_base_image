// Copyright 2025 Daytona Platforms Inc.
// SPDX-License-Identifier: AGPL-3.0

package util

import (
	"bytes"
	"regexp"
)

// ansiEscapeRe 匹配完整的 ANSI 转义序列。
//
// 需要覆盖的形态：
//   - CSI（ESC [ 参数 m）：颜色 / 样式，如 `\x1b[36m`、`\x1b[0m`、`\x1b[1;31m`
//   - CSI 其他终结符：`A/B/C/D`（光标移动）、`J/K`（清屏）、`h/l`（模式设置）等
//   - OSC（ESC ] ... BEL/ST）：终端标题、超链接等，如 `\x1b]8;;url\x1b\\`
//   - 其他单字符 ESC 序列（ESC 7/8/c 等）
//
// 终结符集合取自 ECMA-48 / xterm 常见行为：@A-Z[\]^_（CSI 终结符）与
// a-z~（私有模式 / 其他），一并视为序列结束。
var ansiEscapeRe = regexp.MustCompile(`\x1b(?:\[[0-9;:?>=]*[ -/]*[@-~]|\][^\x1b\x07]*(?:\x07|\x1b\\)|[@-Z\\^_a-z0-9])`)

// StripANSI 移除字符串中的所有 ANSI 转义序列（颜色、光标移动、清屏、OSC 等），
// 仅保留可见文本。用于把可能包含终端控制码的字符串安全地写入日志，避免
// 日志出现 `[36m` / `[0m` 之类的乱码。
func StripANSI(s string) string {
	return ansiEscapeRe.ReplaceAllString(s, "")
}

// StripANSIBytes 是 StripANSI 的 []byte 版本，原地复制后再处理，不改动入参。
func StripANSIBytes(b []byte) []byte {
	return ansiEscapeRe.ReplaceAll(b, []byte{})
}

// SanitizeLogString 对日志输出做统一清洗：
//   - 去掉 ANSI 转义序列
//   - 压缩可能进入日志的二进制不可见字符（\x00-\x08\x0b\x0c\x0e-\x1f），
//     替换为可读的 `\xNN` 形式，避免日志文件出现裸控制字节。
func SanitizeLogString(s string) string {
	s = StripANSI(s)
	var buf bytes.Buffer
	buf.Grow(len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '\t' || c == '\n' || c == '\r':
			// 保留常见的结构字符，避免日志内容粘连
			buf.WriteByte(c)
		case c < 0x20 || c == 0x7f:
			// 其他控制字符转义为可读形式
			buf.WriteString(`\x`)
			const hex = "0123456789abcdef"
			buf.WriteByte(hex[c>>4])
			buf.WriteByte(hex[c&0x0f])
		default:
			buf.WriteByte(c)
		}
	}
	return buf.String()
}
