# 开发文档

## 架构设计

设计文档：`.codeartsdoer/specs/ai_clip_video/design.md`（工具生成目录）与仓库根 [SPEC.md](../SPEC.md)。

### 核心模块
- **Models** (`app/models/`)：领域模型（EDL、Suggestion、RenderPlan、SubtitleConfig 等），时间为整数微秒
- **Services** (`app/services/`)：业务逻辑（pipeline、EDL、render、subtitle、preview_cache、job_runner、project）
- **Providers** (`app/providers/`)：外部适配器（FFmpeg、ASR、VAD、LLM、Avatar、凭据）
- **Persistence** (`app/persistence/`)：全局 SQLite + 每项目 SQLite，含版本化迁移
- **API** (`app/api/`)：REST + WebSocket，全部经 `require_auth`（令牌 + Host + Origin）

### 数据流
```
导入媒体 → 探测(ffprobe/降级) → 抽 16k 单声道 WAV → VAD/音量检测 → (可选) ASR 转录
→ LLM 或规则策略给出建议 → 人工确认 → EDL 修订(可撤销/重做) → RenderPlan(hash)
→ 单趟 FFmpeg 渲染 → 预览缓存 / 导出产物 → 票据化取流播放
```

## 开发环境搭建

### 前置要求
- Python 3.11+
- Node.js 20+
- FFmpeg 6.0+（含 libass；或用 `.[ffmpeg]` 的 imageio-ffmpeg 兜底）
- Rust + WebView2（仅开发桌面外壳时需要）

### 后端 + 前端联调（浏览器）
```bash
pip install -e ".[dev]"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# 另一个终端：Vite 代理会把 /api、/health、/ws 转发到 ACV_PORT 指定的端口
cd frontend
npm ci
set ACV_PORT=8765 && set VITE_AUTH_TOKEN=<数据目录 api.token 的内容> && npm run dev
```
令牌文件默认在 `%LOCALAPPDATA%\auto_clip_video\api.token`；用 `ACV_DATA_DIR` 换数据目录时，
令牌与数据库都会跟着走。
浏览器模式下令牌只能来自 `VITE_AUTH_TOKEN`（或 `frontend/.env.local`），Vite 代理不会注入
`Authorization` 头；写错成 `ACV_AUTH_TOKEN` 的话界面会显示「加载项目失败：缺少或错误的访问令牌」。
`.env.local` 已被 gitignore，但里面是一枚能读你本地媒体路径的令牌，走完查记得删。

### 桌面外壳
```powershell
powershell -File scripts/build_sidecar.ps1   # 产出并放置 sidecar 可执行文件
cd src-tauri
../frontend/node_modules/.bin/tauri.cmd dev  # Tauri CLI 是 frontend 的 devDependency
```
Rust 侧负责启动 sidecar，并通过 `backend_info` 命令把端口与令牌交给前端；
浏览器开发模式下这两项来自环境变量。

两个坑：CLI 必须在 `src-tauri/` 下调用（它按当前目录找 `tauri.conf.json`，在 `frontend/`
里跑会报「不是 Tauri 工程」）；要用 `.cmd` 那一个 shim，`.ps1` 版依赖 PowerShell 7（`pwsh`），
Windows 自带的 5.1 上没有。`cargo tauri` 这种写法需要先 `cargo install tauri-cli`，本仓库不用它。

两条必须成立的约定：

- `SIDECAR_NAME` 只写裸文件名（`auto-clip-video-server`，不带 `binaries/` 前缀）。Tauri v2 把
  `externalBin` 的产物平铺安装到主程序旁边，Rust 侧 `shell().sidecar()` 也按「exe 同级目录 + 该
  名字」解析；写成带前缀的路径会解析到不存在的子目录，而外壳是 `windows_subsystem` 程序，
  setup 返回 Err 的表现就是「双击后闪退、一句提示都没有」。现在失败原因会落到数据目录下的
  `shell-error.log`（默认 `%LOCALAPPDATA%\auto_clip_video\`）。
- 端口与令牌靠数据目录下的文件交接，不走命令行参数。`runtime.port` 只描述**当前**正在运行的
  本地服务：外壳 spawn 之前先删掉它，`backend_info` 等到它出现才返回，避免前端照着上次被强杀
  时留下的旧端口去连。默认端口 8765 被占用时本地服务会退回系统分配的空闲端口（日志事件
  `preferred_port_busy`），所以打包链路上任何一侧都不能把端口写死。
- 退出时必须回收**整棵** sidecar 进程树。PyInstaller 的 onefile 产物是「引导进程 + 真正的
  Python 进程」两个 PID，`CommandChild::kill()` 只打得到引导进程，实测会留下一个继续监听端口、
  占着数据库、还带着 ffmpeg 子进程的孤儿。外壳用 Windows Job Object
  （`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）兜底：句柄一关（正常退出、被强杀、崩溃都一样）内核
  就把整棵树收掉。改成别的回收方式前先确认孤儿进程没有复发。

### 运行测试与检查
```bash
pytest                      # 全部（e2e 需要本机可用 FFmpeg，否则自动 skip）
pytest -m "not e2e"         # 跳过端到端
pytest tests/unit/test_security.py -q
ruff check app tests
cd frontend && npm run typecheck && npm run build
```

## 添加一个 Provider

适配器统一由 `app/providers/registry.py` 的 `Providers` 构造并缓存，**不要**在服务里自己 new：

1. 在 `app/providers/<kind>/` 实现协议（`protocol.py` 里的 `*Protocol`），提供 `is_available()`
2. 在 `Providers.asr / vad / llm / avatar` 的分支里注册，缺失依赖时用 `importlib.util.find_spec` 探测
3. 在 `app/api/pipeline.py` 的 `/capabilities` 里如实上报可用性
4. 前端只依据 `/capabilities` 与 `/avatar/capabilities` 决定入口是否可点

### 未实现能力的写法
必须让「没做」和「失败」在界面上区分开，且都不能返回成功：
- `is_available()` 返回 `False`，`transcribe/synthesize` 抛 `*NotImplementedError`
- 接口返回 503（或 501），`/capabilities` 对应字段为 `false`
- 前端显示不可用原因，不显示假进度条

已按此处理的能力：数字人合成（HeyGen/D-ID，未实现）、WhisperX 与 Qwen ASR（未实现）。
OTIO 导出已放开：产物结构由官方 `opentimelineio` 读取器在 `tests/e2e/test_otio_reader.py`
里验证，升级该库版本时那组测试就是回归门禁（详见 ADR-003）。

## 渲染与画幅约定

- **方向由渲染层负责，不交给 FFmpeg 的自动旋转。** FFmpeg 只在一个输入流被**单个**滤镜消费时才
  自动旋转；多段剪切（`[0:v]trim` 出现两次）会让它退化成「像素不转、只把 displaymatrix 抄到输出」，
  于是竖屏成片以竖屏尺寸携带横屏元数据，显示方向取决于播放器是否识别该标记。
  渲染 argv 固定带 `-noautorotate -display_rotation:v:0 0`，并按 EDL 记录的 `source.rotation`
  插入等效 `transpose` / `hflip,vflip`（与 FFmpeg 自动旋转逐像素一致，SSIM=1.0 实测）。
- **旋转只做一次**：多段时先 `transpose` 再 `split=N` 喂给各条 trim，避免每段重复旋转整帧。
- **yuv420p 要求宽高为偶数**：等比缩放的结果可能是奇数（568x320 → 360x203），libx264 会以
  「Error while opening encoder」直接失败，因此缩放滤镜一律带 `force_divisible_by=2`。
- **画布由布局链产出**：智能裁切 = 填满 + 居中裁剪，模糊背景 = split/boxblur/overlay，
  两种布局对横竖屏都成立；选「源分辨率」时不改画幅，只把边长取偶
  （`scale=trunc(iw/2)*2:trunc(ih/2)*2`，否则 libx264 会拒绝奇数宽高）。
- **探测降级是主路径**：imageio-ffmpeg 只自带 ffmpeg、不带 ffprobe，因此 `ffmpeg -i` 的 stderr
  解析在打包环境里负责给出时长、尺寸、displaymatrix 角度、时间基（`tbn`）与 VFR 判据
  （`fps` 与 `tbr` 的相对偏差）。

## 剪辑决定与时间轴约定

- **边界编辑不改变决定**：`modify` 只挪起止时间，`status` 原样保留。曾经它会把状态改写成
  `modified`，而 `accepted_suggestions()` 只认 `accepted`，于是拖一下边缘就把用户标好的
  「删除」悄悄撤销了；`pending` 被改也会掉出「全部接受」的候选集。
- **合并只在决定一致时继承**：`merge` 后所有被合并区间状态相同才保留该状态，混合
  （accepted + rejected）一律回到 `pending` 重新表态。`split` 的两半直接继承原区间状态。
- **区间不许重叠**：`validate_full()` 把重叠判为结构错误，因此时间轴拖拽在**前端**就要按
  相邻块的边界夹住（左右邻的 end/start），否则松手时只会拿到一个 400。
- **一次手势一条修订**：拖拽期间只更新本地草稿，`pointerup` 才发一次
  `POST /edl/operations`，并带上 `expected_revision_id`；撤销一步就回到这次拖拽之前。
  实测 12 次 pointermove 只产生 1 条修订记录。
- **吸附到整帧**：源 fps 已知时起止时间按 `1e6/fps` 取整，再向相邻边界与 0/末尾吸附
  （阈值 8px）。非整帧切点会让 FFmpeg 在拼接处补/丢帧，成片平均帧率会偏离源（实测 25 → 25.34）。

## 前端约定

- API 客户端：`src/api/client.ts`，令牌来自 `src/api/backend.ts` 的 `bootstrapBackend()`
- 任务进度：`src/hooks/useJobWatch.ts`（WebSocket + 轮询双通道，终态以轮询结果为准）
- 媒体播放：先 `POST /media/stream-ticket` 换短期票据，再交给 `<video>`；令牌不进 URL
- 效果依赖必须是原始值：轮询每 800ms 生成新的 job 对象，依赖对象会重复签票据

## 贡献指南

1. 改动服务或 API 时同步补测试（安全相关必须是行为测试，不接受「源码里有这个字符串」式断言）
2. `ruff check app tests` 与 `pytest` 全绿，前端 `npm run typecheck && npm run build` 通过
3. 涉及打包的改动（spec / tauri.conf / Cargo）需在说明里标注是否真机验证过

## ADR

架构决策记录详见 [docs/adr.md](adr.md)
