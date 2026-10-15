# 运维手册

## 安装与卸载

### 安装
1. 下载 `.msi`（或 `.exe` NSIS 安装包）
2. 双击运行，按向导完成安装（默认 `currentUser` 安装模式，不需要管理员权限）
3. 默认安装路径：`%LOCALAPPDATA%\Programs\AI Clip Video\`

### 卸载
1. 设置 → 应用 → 已安装的应用 → `AI Clip Video` → 卸载
2. 手动清理用户数据：`%LOCALAPPDATA%\auto_clip_video\`
3. 如已保存过 API Key，到「凭据管理器 → Windows 凭据」中删除对应条目

## 目录结构

### 安装目录
```
AI Clip Video/
├── AI Clip Video.exe              # Tauri 主程序
├── auto-clip-video-server.exe     # Python sidecar（由 PyInstaller 产出，随包分发）
└── resources/                     # WebView2 引导与图标等资源
```
FFmpeg 不在安装包内：后端按「`ACV_FFMPEG_PATH` → 系统 PATH → imageio-ffmpeg 自带二进制」的顺序解析，
解析不到时接口会明确报错，不会假装渲染完成。

### 用户数据目录（`%LOCALAPPDATA%/auto_clip_video/`）
```
auto_clip_video/
├── app.db                 # 全局 SQLite（项目索引、偏好设置）
├── api.token              # 本机 API 访问令牌（启动期读取或随机生成）
├── runtime.port           # 实际监听端口，供桌面外壳读取；进程退出时删除
├── logs/app.log           # 结构化 JSON 日志（5 MB × 3 轮转）
├── projects/              # 每个项目自己的 SQLite（EDL 修订、识别结果）
├── uploads/               # 浏览器方式导入的媒体
├── audio_tmp/             # 分析期临时抽取的 16k 单声道 WAV
├── render/                # 渲染中间产物、字幕文件与 OTIO 导出（<project_id>.otio）
├── preview_cache/         # 预览产物（按剪辑方案哈希 LRU 复用）
└── exports/               # 未指定输出路径时的默认导出位置
```
凭据不落这些目录：API Key 存 Windows 凭据管理器；后端不可用时降级为「仅当前进程内存」，
设置页会明确提示重启即失效。

## 配置说明

所有配置项使用 `ACV_` 前缀的环境变量或数据目录同级的 `.env`。常用项：

| 环境变量 | 作用 | 默认值 |
| --- | --- | --- |
| `ACV_DATA_DIR` | 数据目录 | `%LOCALAPPDATA%\auto_clip_video` |
| `ACV_HOST` / `ACV_PORT` | 监听地址与端口 | `127.0.0.1` / `8765`，host 只接受环回地址；该端口已被占用时退回系统分配的空闲端口（实际端口见 `runtime.port`，日志事件 `preferred_port_busy`） |
| `ACV_FFMPEG_PATH` / `ACV_FFPROBE_PATH` | 可执行文件路径 | `ffmpeg` / `ffprobe` |
| `ACV_LOG_LEVEL` / `ACV_LOG_FORMAT` | 日志级别与格式 | `INFO` / `json` |
| `ACV_AUTH_TOKEN` | 固定访问令牌 | 空，即随机生成并写入 `api.token` |
| `ACV_CREDENTIAL_BACKEND` | `auto` 或 `in_memory`（测试用） | `auto` |
| `ACV_MAX_UPLOAD_BYTES` | 上传体积上限 | 4 GiB |
| `ACV_RENDER_TIMEOUT_SECONDS` | 单次 FFmpeg 调用超时 | 900 |
| `ACV_MAX_RENDER_DURATION_MINUTES` | 允许输出的最长时长 | 15 |
| `ACV_ASR_HF_ENDPOINT` | Whisper 模型下载源 | 空（huggingface.co）；网络不可达时填镜像地址，如 `https://hf-mirror.com` |

### ASR 配置
- 引擎只有 `faster_whisper`（需 `pip install -e ".[asr]"`）。未安装时分析会跳过语音识别，
  仅用音量/VAD 检测静音段，并在结果里写明跳过原因。
- 桌面安装包默认不带 ASR，需要 `-WithAsr` 重新出包；`pyinstaller.spec` 里除 `faster_whisper`
  外还要显式收集 `ctranslate2`（它没有 PyInstaller 钩子，原生 DLL 靠包内相对路径加载，
  漏收时打包能成功但冻结产物一加载模型就崩）。
- `.[asr]` 里两个上界是实测钉住的，别随手放开：`setuptools<81`（ctranslate2 4.4 在 import 时
  就用 `pkg_resources`，setuptools 81 起删掉了它）、`av<19`（faster-whisper 给 `av.open` 传
  `metadata_errors`，PyAV 19 已移除该参数，装 19 后第一次识别就 TypeError）。
- WhisperX 与 Qwen ASR 尚未实现，界面上不会出现这两个选项。

### LLM 配置
- 端点 URL、API Key、模型名在「设置」页填写；保存时解析并校验目标地址：
  允许指向本机（`http://127.0.0.1:11434` 这类本地 Ollama/LM Studio），
  明文 `http` 只允许本机，其余地址必须走 HTTPS，且拒绝私网/链路本地/组播/保留网段（防 SSRF）。
- 未配置或调用失败时按规则策略给出建议，并标注「LLM 不可用，已按规则策略」。

### 数字人
- HeyGen / D-ID 未接入真实 API，「数字人」页与相关接口固定返回不可用，不提供试合成。

## 日志查看与问题诊断

### 日志位置
`%LOCALAPPDATA%/auto_clip_video/logs/app.log`（sidecar 无控制台窗口，排障以此文件为准）

### 日志格式
结构化 JSON，包含 `trace_id`、`job_id`、阶段与耗时。用户名、令牌、API Key 在写盘前已被遮蔽为 `<redacted>`。

### 常见问题
1. **界面提示「未取得访问令牌」**：sidecar 未起来或端口被占用。确认 `runtime.port` 存在且端口空闲，
   再看日志里的 `application_starting`。
2. **探测/渲染报 FFmpeg 错误**：日志有 `ffprobe_failed_fallback_to_ffmpeg` 说明 ffprobe 缺失（已自动降级），
   真正的失败原因在同一行的 stderr 摘要里。
3. **导出失败并提示无语音识别结果**：该导出方案要求字幕，但项目没跑过 ASR。改用「无字幕」导出或先分析。
4. **预览反复重新渲染**：预览按剪辑方案哈希缓存，改动 EDL 后哈希变化属正常失效。
5. **OTIO 导出**：产物是数据目录 `render/<project_id>.otio`，只含剪辑决策与 `file://` 媒体路径，
   不含媒体本身；源素材被移动后外部编辑器会报离线。
6. **任务卡在 running**：关闭应用会取消所有后台任务并终止子进程；日志中的 `job_transition` 记录状态变化。

## 数据备份与恢复

### 备份
```powershell
Copy-Item -Recurse "$env:LOCALAPPDATA\auto_clip_video" "D:\backup\auto_clip_video"
```
`projects/` 下是每个项目的 EDL 修订历史，`app.db` 是项目索引，两者要一起备份。
媒体源文件本身不在备份范围内（只记录绝对路径）。

### 恢复
```powershell
Copy-Item -Recurse "D:\backup\auto_clip_video" "$env:LOCALAPPDATA\auto_clip_video"
```
恢复后若源文件移动过位置，重新导入即可；API Key 存在凭据管理器里，不随目录迁移，需要重新填写。

## 构建与发布

```powershell
# 完整安装包（含测试）—— 不含语音识别，约 61 MB
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1

# 带语音识别的安装包（约 139 MB）：先在打包用的干净虚拟环境里装好 .[asr] 再出包
powershell -ExecutionPolicy Bypass -File scripts/build_windows.ps1 -WithAsr

# 只产出并放置 sidecar，用于桌面壳开发（tauri dev）
powershell -ExecutionPolicy Bypass -File scripts/build_sidecar.ps1

# 校验打包产物：sidecar 与安装包内容一致、安装包确实含 sidecar
powershell -ExecutionPolicy Bypass -File scripts/verify_package.ps1
```
`build_windows.ps1` 会依次跑测试、PyInstaller、把产物按 `auto-clip-video-server-<rust-target-triple>.exe`
命名复制到 `src-tauri/binaries/`、构建前端、调用前端 devDependency 里的 Tauri CLI 打包，
最后打印每个安装包的 SHA256 并写入 `dist/SHA256SUMS.txt`。

安装包 SHA256 用于**发布溯源**，不用于可复现构建校验：同一份源码两次构建的产物哈希必然不同
（实测 PyInstaller onefile 每次结果就不一致，WiX 生成的 MSI 还会每次换 PackageCode 并写入时间戳）。
真要承诺可复现构建，需要先做 `SOURCE_DATE_EPOCH`、确定性打包参数与 cargo 的 `remap-path-prefix`。

打包机注意：Anaconda 常带的 `pathlib` 1.0.1 是 Python 2 时代的旧 backport，PyInstaller 检测到它会
直接拒绝运行（`The 'pathlib' package is an obsolete backport ...`）。用干净的解释器建虚拟环境，
或 `pip uninstall pathlib` 后再打包。
