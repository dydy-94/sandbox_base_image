# Download missing .deb packages for offline build
# Triggered by v13-fix8b build failure: imagemagick-7-common and libglycin-2-0
# returned 502 Bad Gateway from aliyun ubuntu pool (chicken/egg: 503/502 on
# busy shared nodes is normal).  Pre-staging them into context/apt-archives/
# guarantees the next build doesn't need to fetch them at all.

$ErrorActionPreference = "Stop"

$archiveRoot = "d:\AIO 新镜像打造\open-source\aio-sandbox\docker\context\apt-archives"
$mirrors = @(
  "http://mirrors.aliyun.com/ubuntu",
  "http://mirrors.cloud.tencent.com/ubuntu",
  "http://mirrors.huaweicloud.com/ubuntu",
  "http://mirrors.ustc.edu.cn/ubuntu",
  "http://mirrors.tuna.tsinghua.edu.cn/ubuntu"
)

# Pairs of (relative pool path, filename) to fetch.  Use --print-uris on
# any external apt to get these.  Pool path is relative to $mirror/ubuntu/.
$targets = @(
  @{ pool = "pool/universe/i/imagemagick"; file = "imagemagick-7-common_7.1.2.18+dfsg1-1_all.deb" },
  @{ pool = "pool/main/g/glycin";          file = "libglycin-2-0_2.1.1+ds-0ubuntu1_amd64.deb" },
  @{ pool = "pool/main/g/glycin";          file = "glycin-loaders_2.1.1+ds-0ubuntu1_amd64.deb" }
)

function Try-Download($url, $dest) {
  Write-Host "  GET $url"
  try {
    Invoke-WebRequest -Uri $url -OutFile $dest -TimeoutSec 30 -UseBasicParsing -ErrorAction Stop
    return $true
  } catch {
    Write-Host "  FAIL ($($_.Exception.Message))"
    return $false
  }
}

foreach ($t in $targets) {
  $finalPath = Join-Path $archiveRoot $t.file
  if (Test-Path $finalPath) {
    $size = (Get-Item $finalPath).Length
    if ($size -gt 1024) {
      Write-Host "OK exists: $($t.file) ($size bytes)"
      continue
    } else {
      Remove-Item $finalPath -Force
    }
  }

  $ok = $false
  foreach ($mirror in $mirrors) {
    $url = "$mirror/$($t.pool)/$($t.file)"
    if (Try-Download $url $finalPath) {
      $sz = (Get-Item $finalPath).Length
      if ($sz -gt 1024) {
        Write-Host "OK from ${mirror}: $([math]::Round($sz/1KB,1)) KB"
        $ok = $true
        break
      } else {
        Write-Host "  Too small, removing ($sz bytes)"
        Remove-Item $finalPath -Force
      }
    }
  }

  if (-not $ok) {
    Write-Error "Failed to download $($t.file) from any mirror"
    exit 1
  }
}

Write-Host ""
Write-Host "Done.  Listing APT archive:"
Get-ChildItem -Path $archiveRoot -Filter "imagemagick*" | Select-Object Name, Length
Get-ChildItem -Path $archiveRoot -Filter "glycin*" | Select-Object Name, Length
Get-ChildItem -Path $archiveRoot -Filter "libglycin*" | Select-Object Name, Length
