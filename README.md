# auto_clip_video — 口播视频自动剪辑工具

Windows 桌面的口播视频剪辑应用：导入长视频 → 检测静音段 → 给出剪辑建议 → 人工确认 → 烧录字幕 → 渲染导出。

界面遵循一条原则：**没实现的能力不会假装成功**。语音识别未安装、LLM 不可用、数字人未接入时，
应用会在界面上写明原因并给出可用的降级路径，而不是显示假的进度或假的产物。

## 当前能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 导入媒体（上传/本地路径） | 可用 | 浏览器方式上传或桌面壳原生选择文件 |
| 静音检测 | 可用 | 音量检测内置；Silero VAD 需 `.[vad]` 额外依赖 |
| 语音识别（字幕/转录） | 可用（需随包依赖） | 仅 `faster-whisper`（`.[asr]`）；安装包要用 `-WithAsr` 才会带上，未随包时跳过并在结果中注明；模型首次使用时下载到数据目录，下载受阻可设 `ACV_ASR_HF_ENDPOINT` 指向镜像 |
| LLM 剪辑决策 | 可选 | 兼容 OpenAI 格式；不可用时按规则策略给建议 |
| 剪辑建议确认 / 手工增删区间 | 可用 | 整数微秒 EDL，支持撤销/重做与版本历史 |
| 时间轴精剪（自研，无第三方库） | 可用 | 拖拽移动/边缘微调/框选新增/合并/播放头处拆分；吸附到整帧与相邻边界，松手才提交一条修订 |
| 原片播放与播放头同步 | 可用 | 短期取流票据喂 `<video>`，时间轴播放头跟随播放位置，点标尺可跳转 |
| 字幕文案人工改写 | 可用 | 覆盖层保存，不改动机器识别的原始转录；可单条恢复自动文本 |
| 字幕样式配置 | 可用 | 颜色/描边/阴影/粗斜体/边距与安全区，可预览生成的 .ass 文本 |
| 音轨选择 | 可用 | 多音轨素材可指定识别与导出使用同一条音轨（识别与导出不同轨会毁掉字幕对齐） |
| 字幕生成（ASS/SRT）与烧录 | 可用 | 依赖 FFmpeg 的 libass |
| 预览与缓存 | 可用 | 按剪辑方案哈希做 LRU 缓存 |
| 导出 720p/1080p/源分辨率 | 可用 | 竖屏 9:16 或 16:9，模糊背景填充或智能裁切；方向按源素材标注转正，成片不自带旋转元数据 |
| 导出帧率与画质 CRF | 可用 | 帧率 25/30/60 或跟随源；CRF 14–35 可调，预览固定用快速档 |
| OTIO 时间线导出 | 可用 | 单轨剪辑交换文件（微秒时间基、`file://` 媒体引用）；产物由官方 opentimelineio 读取器在 e2e 测试里打开并逐字段比对 |
| 数字人合成（HeyGen / D-ID） | 未实现 | 页面与接口都明确报告不可用 |
| WhisperX / Qwen ASR | 未实现 | 不出现在引擎选项里 |

## 快速开始（源码运行）

```bash
# 后端（默认监听 127.0.0.1:8765，数据目录 %LOCALAPPDATA%\auto_clip_video）
pip install -e ".[dev]"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 前端（另一个终端）
cd frontend && npm install && npm run dev
```

后端启动时会在数据目录生成随机访问令牌 `api.token`。浏览器里跑前端需要把它交给 Vite：

```bash
set ACV_AUTH_TOKEN=<api.token 的内容> && set ACV_PORT=8765 && npm run dev
```

桌面外壳在 `src-tauri/` 下执行 `../frontend/node_modules/.bin/tauri.cmd dev`（Tauri CLI 是前端的
devDependency），由 Rust 侧启动 sidecar 并读取端口与令牌，无需手工配置；
运行前需先执行 `powershell -File scripts/build_sidecar.ps1` 产出 sidecar 可执行文件。

## 安装

1. 下载安装包（`.msi` 或 NSIS `.exe`），默认按当前用户安装到
   `%LOCALAPPDATA%\Programs\AI Clip Video\`
2. FFmpeg 不在安装包内：程序按 `ACV_FFMPEG_PATH` → 系统 PATH → imageio-ffmpeg 自带二进制 的顺序查找
3. 用户数据在 `%LOCALAPPDATA%\auto_clip_video\`（项目库、缓存、日志、默认导出目录）

详见 [docs/operations.md](docs/operations.md)。

## 技术栈

- **后端** Python 3.11 + FastAPI + Pydantic v2 + SQLAlchemy 2（async）+ structlog
- **前端** React 18 + TypeScript + Vite + Zustand + Ant Design
- **桌面** Tauri v2（Python 后端以 PyInstaller 打成 sidecar）
- **媒体** FFmpeg 6+（含 libass），单趟 `-filter_complex` 完成剪切/拼接/缩放/字幕

## 开发

```bash
ruff check app tests
pytest                       # 全部
pytest -m "not e2e"          # 跳过需要 FFmpeg 的端到端用例
cd frontend && npm run typecheck && npm run build
```

- 架构与决策记录：[docs/adr.md](docs/adr.md)、[SPEC.md](SPEC.md)
- 开发环境与调试：[docs/development.md](docs/development.md)
- 常见问题：[docs/faq.md](docs/faq.md)
- 打包发布：`scripts/build_windows.ps1`

## 许可证

详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
