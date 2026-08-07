# 静态校验：解析 Dockerfile.final 里所有 COPY/ARG，验证源路径存在
$ErrorActionPreference = 'Stop'
$root = "D:\AIO-GIT\sandbox_base_image\x-open-no-root-k8s\open-source-final-version\aio-sandbox"
$dockerfile = Join-Path $root "Dockerfile.final"

$missing = @()
$copyCount = 0
$lineNum = 0

Get-Content $dockerfile | ForEach-Object {
  $lineNum++
  $line = $_

  # 只看 COPY (不含 COPY --from=)
  if ($line -match '^\s*COPY\s+(?!--from=)(.+)\s+(.+)\s*$') {
    $copyCount++
    $srcs = $matches[1] -split '\s+'
    foreach ($s in $srcs) {
      $fullPath = Join-Path $root $s
      # 是 URL 或 envsubst 的跳过
      if ($s -match '^\$' -or $s -match '^[/-]' -or $s -match 'http') { continue }
      if (-not (Test-Path $fullPath)) {
        $missing += "L${lineNum}: COPY $s -> $fullPath (NOT FOUND)"
      }
    }
  }
}

Write-Host "Total COPY lines checked: $copyCount"
Write-Host "Missing sources: $($missing.Count)"
$missing | ForEach-Object { Write-Host "  $_" }