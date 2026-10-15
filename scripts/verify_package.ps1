# verify_package.ps1 — 校验打包产物的完整性：sidecar 被忠实复制、安装包确实把它装进去了。
#
#   powershell -ExecutionPolicy Bypass -File scripts/verify_package.ps1
#
# 这里刻意不做「可复现构建」校验：实测同一份 pyinstaller.spec 两次 --clean 构建出来的
# SHA256 就不一致，WiX 生成的 MSI 每次还会换 PackageCode 并写入构建时间戳，
# 因此比对安装包哈希永远不会通过。真要承诺可复现，需要 SOURCE_DATE_EPOCH、
# 确定性打包参数与 cargo 的 remap-path-prefix，目前不在范围内。

param(
    [string]$OutputDir = "dist"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RootDir = Resolve-Path "$PSScriptRoot/.."
Set-Location $RootDir

if (-not (Get-Command rustc -ErrorAction SilentlyContinue)) {
    throw "需要 rustc 解析 target triple，请先安装 Rust。"
}
$triple = ((& rustc -vV | Select-String '^host:') -replace '^host:\s*', '').Trim()
if (-not $triple) { throw "无法从 rustc -vV 解析 host triple。" }

Write-Host "[1/2] 比对 sidecar（PyInstaller 产物 vs src-tauri/binaries）..." -ForegroundColor Cyan
$built = Join-Path $RootDir "$OutputDir/sidecar/auto-clip-video-server.exe"
$staged = Join-Path $RootDir "src-tauri/binaries/auto-clip-video-server-$triple.exe"
if (-not (Test-Path $built)) { throw "缺少 PyInstaller 产物: $built" }
if (-not (Test-Path $staged)) { throw "缺少已放置的 sidecar: $staged（先跑 build_sidecar.ps1）" }

$hashBuilt = (Get-FileHash $built -Algorithm SHA256).Hash
$hashStaged = (Get-FileHash $staged -Algorithm SHA256).Hash
if ($hashBuilt -ne $hashStaged) {
    throw "binaries 里的 sidecar 与最新一次 PyInstaller 产物不一致：复制步骤漏了或已过期。"
}
Write-Host "  一致 SHA256: $hashBuilt" -ForegroundColor Green

Write-Host "[2/2] 检查安装包是否真的含 sidecar..." -ForegroundColor Cyan
$bundleRoot = Join-Path $RootDir "src-tauri/target/release/bundle"
$packages = @(
    Get-ChildItem -Path (Join-Path $bundleRoot "msi/*.msi") -ErrorAction SilentlyContinue
    Get-ChildItem -Path (Join-Path $bundleRoot "nsis/*-setup.exe") -ErrorAction SilentlyContinue
)
if (-not $packages) { throw "未找到安装包，先跑 build_windows.ps1。" }

foreach ($pkg in $packages) {
    $mb = [math]::Round($pkg.Length / 1MB, 1)
    # 只有 Tauri 外壳约 10 MB；sidecar 本身 50 MB 以上，装进去才会明显超过阈值
    if ($pkg.Length -lt 20MB) {
        throw "$($pkg.Name) 只有 $mb MB，sidecar 很可能没被打进安装包。"
    }
    Write-Host ("  {0}  {1} MB  SHA256={2}" -f $pkg.Name, $mb, (Get-FileHash $pkg.FullName -Algorithm SHA256).Hash)
}

Write-Host "`n打包产物校验通过。" -ForegroundColor Green
