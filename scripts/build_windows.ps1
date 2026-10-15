# build_windows.ps1 — Windows 一键构建脚本（源码 → sidecar → 安装包）。
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1 -WithFfmpeg -WithAsr
#
# 前置条件: Python 3.11+、Node 20+、Rust（含 cargo）+ Visual Studio C++ 生成工具，
# 以及 WiX 3.x（MSI 打包需要，Tauri 会自动下载）。
# Tauri CLI 由 frontend 的 devDependency 提供，不需要 cargo install tauri-cli。

param(
    [string]$Version = "0.1.0",
    [string]$OutputDir = "dist",
    [switch]$WithFfmpeg,
    [switch]$WithAsr,
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$ProgressPreference = "SilentlyContinue"

function Step($n, $total, $msg) {
    Write-Host "`n[$n/$total] $msg" -ForegroundColor Cyan
}

function Assert-LastExit($what) {
    if ($LASTEXITCODE -ne 0) {
        throw "$what 失败（退出码 $LASTEXITCODE）"
    }
}

function Require-Tool($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "找不到 $name。$hint"
    }
}

Write-Host "=== auto_clip_video Windows Build ===" -ForegroundColor Cyan
Write-Host "Version: $Version"

$RootDir = Resolve-Path "$PSScriptRoot/.."
Set-Location $RootDir

Require-Tool python "请先安装 Python 3.11+ 并加入 PATH。"
Require-Tool npm "请先安装 Node 20+ 并加入 PATH。"

$extras = @("dev")
if ($WithFfmpeg) { $extras += "ffmpeg" }
if ($WithAsr) { $extras += "asr" }
# 多个 extras 是同一个方括号里的逗号列表：写成 .[dev],[asr] 会被 pip 判成
# "Invalid requirement: Expected package name at the start of dependency specifier"，
# 所以 -WithAsr / -WithFfmpeg 一旦同时出现（或任意两个组合）第 2 步就会失败。
$extrasArg = ".[" + ($extras -join ",") + "]"

$steps = 7

Step 1 $steps "检查打包工具链..."
Require-Tool cargo "请先安装 Rust（rustup），否则无法产出安装包。"
$triple = ((& rustc -vV | Select-String '^host:') -replace '^host:\s*', '').Trim()
if (-not $triple) { throw "无法从 rustc -vV 解析 host triple。" }
Write-Host "  Rust target triple: $triple"

Step 2 $steps "安装 Python 依赖 ($extrasArg)..."
python -m pip install --upgrade pip
Assert-LastExit "pip 升级"
pip install -e $extrasArg pyinstaller
Assert-LastExit "Python 依赖安装"

# Anaconda 等环境常带 pathlib 这个 Python 2 时代的旧 backport，PyInstaller 检测到它会直接
# 拒绝运行并抛一句很难定位的报错；在这里拦下并给出可执行的修复命令。
# 走 cmd 重定向：pip 找不到包时把 WARNING 写到 stderr，而 ErrorActionPreference=Stop 会把
# 原生命令的 stderr 当成终止错误，直接写 2>$null 会在"环境干净"的机器上误杀构建。
cmd /c "python -m pip show pathlib >NUL 2>&1"
if ($LASTEXITCODE -eq 0) {
    throw "检测到过时的 pathlib 包，PyInstaller 会拒绝运行。请执行: python -m pip uninstall -y pathlib"
}
# pyinstaller 是上一步才装的，检查必须放在安装之后，否则干净机器上必然误报缺失
Require-Tool pyinstaller "pip 安装后仍找不到 pyinstaller，请确认 Scripts 目录在 PATH 中。"

Step 3 $steps "运行测试..."
if ($SkipTests) {
    Write-Host "  已按 -SkipTests 跳过" -ForegroundColor Yellow
} else {
    pytest --tb=short -q
    Assert-LastExit "测试"
}

Step 4 $steps "构建 Python sidecar..."
pyinstaller pyinstaller.spec --noconfirm --clean --distpath "$OutputDir/sidecar" --workpath build/pyinstaller
Assert-LastExit "PyInstaller"

$sidecarExe = Join-Path $RootDir "$OutputDir/sidecar/auto-clip-video-server.exe"
if (-not (Test-Path $sidecarExe)) {
    throw "PyInstaller 产物缺失: $sidecarExe"
}

Step 5 $steps "复制 sidecar 到 src-tauri/binaries（Tauri 要求 <name>-<triple>.exe 命名）..."
$binDir = Join-Path $RootDir "src-tauri/binaries"
New-Item -ItemType Directory -Force -Path $binDir | Out-Null
$sidecarTarget = Join-Path $binDir "auto-clip-video-server-$triple.exe"
Copy-Item $sidecarExe $sidecarTarget -Force
Write-Host "  $sidecarTarget"

Step 6 $steps "构建前端与安装包..."
Push-Location frontend
try {
    # npm ci 会先删掉 node_modules 再装：残留的 vite/esbuild 进程会锁住原生模块，
    # 让它以 EPERM 失败并把依赖清空，所以先挡一道。
    # 按命令行匹配而不是进程名——Vite 在 Windows 上跑起来叫 node.exe，
    # 只按名字找会漏掉，而按 "node" 杀又会误伤无关的 Node 程序。
    $projectRoot = (Resolve-Path $RootDir).Path
    $stale = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^(node|esbuild)\.exe$' -and $_.CommandLine -and
        $_.CommandLine -like "*$projectRoot*" -and $_.CommandLine -match 'vite|esbuild'
    })
    if ($stale) {
        $list = ($stale | ForEach-Object { "$($_.Name) PID $($_.ProcessId)" }) -join '; '
        throw "检测到本项目的前端开发服务仍在运行（$list），请先停止再打包。"
    }
    npm ci
    Assert-LastExit "npm ci"
    npm run build
    Assert-LastExit "前端构建"
    if (-not (Test-Path "dist/index.html")) { throw "前端产物缺失: frontend/dist/index.html" }
} finally {
    Pop-Location
}

$tauriCli = Join-Path $RootDir "frontend/node_modules/.bin/tauri.cmd"
if (-not (Test-Path $tauriCli)) {
    throw "缺少 Tauri CLI: $tauriCli（frontend 的 devDependency，需先 npm ci）"
}

Push-Location src-tauri
try {
    # CLI 以 src-tauri 为工作目录解析工程；在 frontend 目录里调用会报「不是 Tauri 工程」，
    # 而 `cargo tauri` 需要另外 cargo install tauri-cli，这里用 npm 装的那份
    & $tauriCli build
    Assert-LastExit "Tauri 打包"
} finally {
    Pop-Location
}

Step 7 $steps "校验产物..."
$packages = @()
foreach ($pattern in @("src-tauri/target/release/bundle/msi/*.msi", "src-tauri/target/release/bundle/nsis/*.exe")) {
    $packages += Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue
}
if (-not $packages) {
    throw "未找到安装包，请检查上面的 Tauri 输出。"
}
$lines = [string[]]@(foreach ($pkg in $packages) {
    $hash = (Get-FileHash $pkg.FullName -Algorithm SHA256).Hash
    Write-Host "  $($pkg.Name)  $([math]::Round($pkg.Length / 1MB, 1)) MB" -ForegroundColor Green
    Write-Host "    SHA256: $hash"
    # 记相对仓库根的路径：两个安装包分处 msi/ 与 nsis/ 两个目录，
    # 只记裸文件名的话 sha256sum -c 在任何目录下都解析不到
    $rel = $pkg.FullName.Substring($RootDir.Path.Length).TrimStart('\', '/').Replace('\', '/')
    "$hash  $rel"
})
# 每次重写而不是追加：追加会让同一个文件名对应多个哈希，发布时分不清哪个算数。
# 必须无 BOM 且用 LF 换行：PowerShell 的 -Encoding utf8 会加 BOM，而
# [IO.File]::WriteAllLines 写 CRLF，sha256sum -c 会把行尾的 \r 当成文件名的一部分。
$sumsPath = Join-Path $RootDir "$OutputDir/SHA256SUMS.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $sumsPath) | Out-Null
[IO.File]::WriteAllText($sumsPath, (($lines -join "`n") + "`n"))

Write-Host "`n=== Build complete ===" -ForegroundColor Cyan
Write-Host "安装包: src-tauri/target/release/bundle/"
Write-Host "校验和: $OutputDir/SHA256SUMS.txt"
