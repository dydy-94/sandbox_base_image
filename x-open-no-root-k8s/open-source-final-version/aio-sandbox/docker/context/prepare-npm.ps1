# prepare-npm.ps1 — Windows-native equivalent of prepare-npm.sh
#
# This script exists because Git-bash's MSYS path translation breaks
# `command -v node.exe` when invoked via Python subprocess on Windows.
# Using PowerShell directly avoids the bash subprocess overhead entirely.
#
# It does the same work as prepare-npm.sh:
#   1. For each package in context/aio/package.json and
#      context/static-assets/package.json, run `npm pack` to produce
#      a .tgz in the destination directory.
#   2. For bun, also `npm pack bun@<version>`.
#
# The output layout matches prepare-npm.sh, so Dockerfile.offline's
# `COPY context/npm-tgz/<bundle>/` lines work unchanged.
#
# Usage: powershell -ExecutionPolicy Bypass -File prepare-npm.ps1

$ErrorActionPreference = 'Continue'   # Never abort the whole run on one
                                     # missing tarball; print a warn instead.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = (Resolve-Path "$ScriptDir\..\..").Path
$ctx = "$RepoRoot\docker\context"
$outRoot = "$ctx\npm-tgz"
$aioOut = "$outRoot\aio"
$staticOut = "$outRoot\static-assets"
$bunOut = "$outRoot\bun"
New-Item -ItemType Directory -Force -Path $aioOut, $staticOut, $bunOut | Out-Null
# `.keep` files ensure each dir exists if no tgz landed there.
'' | Set-Content -Path "$aioOut\.keep"
'' | Set-Content -Path "$staticOut\.keep"
'' | Set-Content -Path "$bunOut\.keep"

$NPM_REGISTRY = if ($env:NPM_REGISTRY) { $env:NPM_REGISTRY } else { 'https://registry.npmmirror.com' }

# Pick the npm on PATH. On Windows, Get-Command matches the *first*
# `.ps1` flavor ("ExternalScript: npm.ps1") rather than the npm.cmd
# wrapper. Either works for our purpose — we'd rather just look up
# `node.exe` directly and shell out via `node <script>` or rely on
# the standard PATH that npm.cmd is already on.
$nodeCmd = Get-Command node.exe -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    Write-Host "ERROR: no node on PATH; install Node.js 20+ first." -ForegroundColor Red
    exit 1
}
$npmCmd = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    Write-Host "ERROR: no npm on PATH; install Node.js 20+ first." -ForegroundColor Red
    exit 1
}
# Prefer npm.exe / npm.cmd — these are what we actually call. (npm
# resolution via the .ps1 wrapper can fail on ExecutionPolicy when
# running from another PowerShell session.)
$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    $npmCmd = Get-Command npm -ErrorAction SilentlyContinue
}
Write-Host "Using npm: $($npmCmd.Source)"
Write-Host "Using node: $($nodeCmd.Source)"
Write-Host "NPM_REGISTRY: $NPM_REGISTRY"

function Pack-PackageList {
    param(
        [string]$PackageJsonDir,
        [string]$OutDir,
        [string]$Label
    )
    if (-not (Test-Path "$PackageJsonDir\package.json")) {
        Write-Host "[pack] ${Label}: no package.json at $PackageJsonDir" -ForegroundColor Yellow
        return
    }
    Write-Host "=== npm pack for $Label ==="

    # Build a list of deps from package.json + package-lock.json (if present).
    # The Node script is written to a temp file so we don't have to
    # worry about PowerShell argument-quoting rules — passing a
    # multi-line script via -e is fragile across PowerShell hosts.
    $scriptPath = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), ('enumerate_' + [Guid]::NewGuid().ToString('N') + '.js'))
    Write-Host "[pack] ${Label}: writing helper at $scriptPath"
    $js = @'
const fs = require('fs');
const path = require('path');
// argv[0] = node, argv[1] = this script, argv[2..] = the rest of the
// command line. PowerShell's `node foo.js -- bar` puts bar in argv[2]
// AFTER the literal "--" so we need to search past it.
let dir = null;
for (let i = 2; i < process.argv.length; i++) {
    if (!process.argv[i].startsWith('-')) {
        dir = process.argv[i];
        break;
    }
}
if (!dir) {
    process.stderr.write('no dir argument\n');
    process.exit(2);
}
try {
    const lock = JSON.parse(fs.readFileSync(path.join(dir, 'package-lock.json'), 'utf8'));
    const packages = lock.packages || {};
    const seen = new Set();
    const items = [];
    for (const k of Object.keys(packages)) {
        if (k === '') continue;                  // skip root entry
        const p = packages[k];
        if (!p) continue;
        // In npm lockfileVersion 3+, the `name` field is omitted on
        // most entries — extract from key path as a fallback.
        const name = p.name || k.replace(/^node_modules\//, '').replace(/\/+$/, '');
        const version = p.version;
        if (!name || !version) continue;
        const key = name + '@' + version;
        if (seen.has(key)) continue;
        seen.add(key);
        items.push(key);
    }
    items.sort();
    console.log(items.join('\n'));
} catch (e) {
    // No lockfile: fall back to declared deps in package.json.
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    const all = Object.assign({}, pkg.dependencies || {}, pkg.devDependencies || {});
    const items = [];
    for (const k of Object.keys(all)) items.push(k + '@' + all[k]);
    console.log(items.join('\n'));
}
'@
    Set-Content -Path $scriptPath -Value $js -NoNewline -Encoding UTF8
    try {
        # Write to file from PS then run node directly with stdout capture.
        Write-Host "[pack] ${Label}: helper at $scriptPath"
        $nodeOutput = & node $scriptPath -- "$PackageJsonDir" 2>&1
        $nodeExit = $LASTEXITCODE
        $depsList = ($nodeOutput | Out-String).TrimEnd()
        if ($nodeExit -ne 0) {
            Write-Host "[pack] ${Label}: node exited with code $nodeExit" -ForegroundColor Yellow
            Write-Host "[pack] node output: $depsList" -ForegroundColor Yellow
            return
        }
        Write-Host "[pack] ${Label}: depsList length=$($depsList.Length), lines=$($depsList -split "`r?`n").Count"
    } finally {
        # Keep the file for debugging; comment out the rm if you want.
        # Remove-Item $scriptPath -Force -ErrorAction SilentlyContinue
    }

    $lines = $depsList -split "`n"
    Write-Host "[pack] ${Label}: $($lines.Count) deps to pack"
    if ($lines.Count -eq 0 -or ($lines.Count -eq 1 -and [string]::IsNullOrWhiteSpace($lines[0]))) {
        Write-Host "[pack] ${Label}: depsList was empty, dumping:" -ForegroundColor Yellow
        Write-Host $depsList
        return
    }

    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        Write-Host "[pack] $line"
        # Use `& cmd /c npm.cmd` rather than `& npm pack` directly. The
        # latter resolves through PowerShell's alias table which often
        # picks the .ps1 wrapper; the .cmd wrapper is what we want.
        $cmd = "npm pack --registry `"$NPM_REGISTRY`" --pack-destination `"$OutDir`" `"$line`""
        $packOut = cmd.exe /c $cmd 2>&1
        $packRc = $LASTEXITCODE
        if ($packRc -ne 0) {
            $cmd2 = "npm pack --registry `https://registry.npmjs.org` --pack-destination `"$OutDir`" `"$line`""
            $packOut2 = cmd.exe /c $cmd2 2>&1
            $packRc2 = $LASTEXITCODE
            if ($packRc2 -ne 0) {
                Write-Host "[pack] WARN: $line failed; build-time install will retry" -ForegroundColor Yellow
            }
        }
    }
}

# === aio bundle ===
Pack-PackageList -PackageJsonDir "$ctx\aio" -OutDir $aioOut -Label "aio"

# === static-assets bundle ===
Pack-PackageList -PackageJsonDir "$ctx\static-assets" -OutDir $staticOut -Label "static-assets"

# === bun global tarball ===
Write-Host "=== bun global tarball ==="
$BunVersion = if ($env:BUN_VERSION) { $env:BUN_VERSION } else { '1.3.14' }
$bunPack = & npm pack --registry $NPM_REGISTRY --pack-destination $bunOut "bun@${BunVersion}" 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARN: failed to fetch bun@${BunVersion}; build will try network" -ForegroundColor Yellow
}

# === Summary ===
Write-Host "=== Summary ==="
foreach ($d in @($aioOut, $staticOut, $bunOut)) {
    $name = Split-Path $d -Leaf
    $n = (Get-ChildItem $d -Filter '*.tgz' -ErrorAction SilentlyContinue | Measure-Object).Count
    $s = (Get-ChildItem $d -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $sz = if ($s) { "{0:N1} MB" -f ($s / 1MB) } else { "0" }
    Write-Host ("  {0,-22} {1,3} files, {2}" -f $name, $n, $sz)
}
Write-Host "Done. Run docker buildx build -f docker/Dockerfile.offline ..."
