# build_sidecar.ps1 — 只构建并放置 Python sidecar，供桌面壳 `tauri dev` 使用。
#
#   powershell -ExecutionPolicy Bypass -File scripts/build_sidecar.ps1
#
# tauri dev 也会校验 bundle.externalBin 列出的文件是否存在，
# 所以本地跑桌面壳之前必须先执行一次本脚本（或完整的 build_windows.ps1）。

param(
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) { throw "$what 失败（退出码 $LASTEXITCODE）" }
}

$RootDir = Resolve-Path "$PSScriptRoot/.."
Set-Location $RootDir

foreach ($tool in @("pyinstaller", "rustc")) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "找不到 $tool，请先安装（PyInstaller / Rust 工具链）。"
    }
}

Write-Host "[1/3] PyInstaller 打包 sidecar..." -ForegroundColor Cyan
pyinstaller pyinstaller.spec --noconfirm --clean --distpath "$OutputDir/sidecar" --workpath build/pyinstaller
Assert-LastExit "PyInstaller"

$exe = Join-Path $RootDir "$OutputDir/sidecar/auto-clip-video-server.exe"
if (-not (Test-Path $exe)) { throw "产物缺失: $exe" }

Write-Host "[2/3] 解析 Rust target triple..." -ForegroundColor Cyan
$triple = ((& rustc -vV | Select-String '^host:') -replace '^host:\s*', '').Trim()
if (-not $triple) { throw "无法解析 host triple。" }

Write-Host "[3/3] 复制到 src-tauri/binaries/auto-clip-video-server-$triple.exe" -ForegroundColor Cyan
$binDir = Join-Path $RootDir "src-tauri/binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
Copy-Item $exe (Join-Path $binDir "auto-clip-video-server-$triple.exe") -Force
Write-Host "完成。在 src-tauri 目录执行 ../frontend/node_modules/.bin/tauri dev 即可启动桌面壳。" -ForegroundColor Green
