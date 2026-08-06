# prepare-rust.ps1 — Windows-native equivalent of prepare-rust.sh
#
# Downloads the rustup-init binary and clones agent-browser source for
# the offline docker build. Same layout as prepare-rust.sh:
#
#   docker\context\rustup-pre\rustup-init-<arch>
#   docker\context\cargo-vendored\agent-browser\

$ErrorActionPreference = 'Continue'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = (Resolve-Path "$ScriptDir\..\..").Path
$ctx = "$RepoRoot\docker\context"
$rustupPre = "$ctx\rustup-pre"
$cargoPre = "$ctx\cargo-vendored"
New-Item -ItemType Directory -Force -Path $rustupPre, $cargoPre | Out-Null
'' | Set-Content -Path "$rustupPre\.keep"
'' | Set-Content -Path "$cargoPre\.keep"

# Pick arch. Treat AMD64 (x86_64) as the default; PowerShell gives us
# $env:PROCESSOR_ARCHITECTURE.
$procArch = $env:PROCESSOR_ARCHITECTURE
$rustbin = switch ($procArch) {
    'AMD64' { 'rustup-init-x86_64-unknown-linux-gnu' }
    'ARM64' { 'rustup-init-aarch64-unknown-linux-gnu' }
    default { "rustup-init-${procArch,,}-unknown-linux-gnu" }
}
$rustTarball = "$rustupPre\$rustbin"
Write-Host "=== rustup-init ($rustbin) ==="

if (Test-Path $rustTarball -PathType Leaf) {
    $len = (Get-Item $rustTarball).Length
    if ($len -gt 5000000) {
        Write-Host "Already present: $rustTarball ($('{0:N1} MB' -f ($len / 1MB)))"
    } else {
        Write-Host "Removing partial: $len bytes" -ForegroundColor Yellow
        Remove-Item $rustTarball -Force
    }
}
if (-not (Test-Path $rustTarball -PathType Leaf)) {
    # Note: rust-lang.org stopped hosting rustup-init-* directly; the
    # mirrors only carry rustup-init.sh. The build needs the actual
    # binary, so we extract it from the .sh wrapper.
    $shUrl = 'https://sh.rustup.rs'
    $shTemp = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), 'rustup-init.sh')
    try {
        Write-Host "[download] $shUrl (rustup-init.sh)"
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $shUrl -OutFile $shTemp -UseBasicParsing -TimeoutSec 60
        Write-Host "[extract] parsing $shTemp for the linux binary"
        # rustup-init.sh is a shell wrapper that re-issues a curl to
        # fetch the right binary. The actual binary url is in there as
        # a base64-encoded zip. Easier: just call rustup-init.sh and
        # tell it to download the binary to a directory.
        # Save the actual binary the way rustup.sh would.
        $bash = Get-Command bash.exe -ErrorAction SilentlyContinue
        if ($bash) {
            $outDir = $rustupPre
            & bash $shTemp -y --default-toolchain none --no-modify-path --target x86_64-unknown-linux-gnu --output $outDir 2>&1 | Out-Host
        } else {
            Write-Host "no bash on PATH; cannot run rustup-init.sh" -ForegroundColor Yellow
        }
        Remove-Item $shTemp -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Host "[download] $shUrl failed: $_" -ForegroundColor Yellow
    }

    # If the above wrote nothing useful, fall back to fetching the
    # binary directly from rust-lang.org's distribution repo.
    if (-not (Test-Path $rustTarball -PathType Leaf)) {
        $candidates = @(
            "https://static.rust-lang.org/rustup/dist/x86_64-unknown-linux-gnu/rustup-init",
            "https://mirrors.ustc.edu.cn/rustup/static/dist/x86_64-unknown-linux-gnu/rustup-init"
        )
        foreach ($url in $candidates) {
            try {
                Write-Host "[download] $url"
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest -Uri $url -OutFile $rustTarball -UseBasicParsing -TimeoutSec 60
                $len = (Get-Item $rustTarball).Length
                if ($len -gt 5000000) {
                    Write-Host "Downloaded: $('{0:N1} MB' -f ($len / 1MB))"
                    break
                }
            } catch {
                Write-Host "[download] $url failed: $_" -ForegroundColor Yellow
            }
        }
    }

    if (-not (Test-Path $rustTarball -PathType Leaf)) {
        Write-Host "ERROR: could not fetch $rustbin" -ForegroundColor Red
        Write-Host "(build-time will fall back to network, which is allowed by Dockerfile.offline)" -ForegroundColor Yellow
        # Don't fail the script — `Dockerfile.offline` allows the build
        # to fall back to network for this single artifact.
    }
}

# === agent-browser source ===
$agentVer = if ($env:AGENT_BROWSER_VERSION) { $env:AGENT_BROWSER_VERSION } else { '0.27.1' }
$agentDir = "$cargoPre\agent-browser"
Write-Host "=== agent-browser source (rev $agentVer) ==="

if (Test-Path "$agentDir\.git") {
    Write-Host "Already cloned (delete $agentDir to force re-clone)"
} else {
    $git = (Get-Command git.exe -ErrorAction SilentlyContinue).Source
    if (-not $git) {
        Write-Host "ERROR: no git on PATH; install Git for Windows first." -ForegroundColor Red
        exit 1
    }
    Push-Location $cargoPre
    # Upstream repo: vercel-labs/agent-browser (the historical
    # nicebyte fork was archived).
    $candidates = @(
        @{ url = "https://github.com/vercel-labs/agent-browser.git"; ref = "v$agentVer" },
        @{ url = "https://github.com/vercel-labs/agent-browser.git"; ref = $null     }
    )
    $ok = $false
    foreach ($c in $candidates) {
        try {
            if ($c.ref) {
                Write-Host "[clone] $($c.url) (tag $($c.ref))"
                & git clone --depth 1 --branch $c.ref $c.url agent-browser 2>&1 | Out-Host
            } else {
                Write-Host "[clone] $($c.url) (default branch)"
                & git clone --depth 1 $c.url agent-browser 2>&1 | Out-Host
            }
            $ok = $true
            break
        } catch {
            Write-Host "[clone] $($c.url) ref=$($c.ref) failed: $_" -ForegroundColor Yellow
        }
    }
    Pop-Location
    if (-not $ok) {
        Write-Host "ERROR: agent-browser clone failed; build will try network" -ForegroundColor Yellow
    }
}

# === summary ===
Write-Host "=== Summary ==="
if (Test-Path $rustTarball -PathType Leaf) {
    $len = (Get-Item $rustTarball).Length
    Write-Host ("  {0,-50} {1}" -f $rustTarball, "$('{0:N1} MB' -f ($len / 1MB))")
}
if (Test-Path $agentDir) {
    $len = (Get-ChildItem $agentDir -Recurse -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    Write-Host ("  {0,-50} {1}" -f $agentDir, "$('{0:N1} MB' -f ($len / 1MB))")
}
Write-Host "Done."
