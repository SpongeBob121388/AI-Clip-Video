# FAQ — 常见问题

## 安装相关

### Q: 安装后无法启动？
A: 需要 Windows 10/11 与 WebView2 运行时（安装包默认会联网引导安装 WebView2）。
若企业环境禁止联网，请预先安装 Microsoft Edge WebView2 Runtime。

### Q: 双击后窗口一闪就没 / 窗口里显示「本地服务未能启动」？
A: 外壳是无控制台程序，本地服务起不来时不会弹窗报错，只留两份文件在
`%LOCALAPPDATA%\auto_clip_video\`：
- `shell-error.log`：外壳找不到或拉不起 `auto-clip-video-server.exe`（多为安装目录被安全软件拦截）
- `logs\app.log`：本地服务自身的启动记录，正常启动会依次写入 `global_db_initialized`、
  `application_starting`。窗口里那句提示也带着这个日志的路径。端口 8765 被别的进程占用时会自动
  改用空闲端口（事件 `preferred_port_busy`），这不是故障、不需要手工处理

### Q: 安装包包含哪些东西？
A: Tauri 外壳 + PyInstaller 打出的 Python 后端。**不包含** FFmpeg、ASR 模型与字体：
- FFmpeg 按 `ACV_FFMPEG_PATH` → 系统 PATH → imageio-ffmpeg 自带二进制 的顺序查找
- 语音识别是**构建期**决定的：默认安装包不含 `faster-whisper`，分析时会写明"已跳过语音识别"。
  要随包就必须在打包机上先装好依赖再跑 `scripts/build_windows.ps1 -WithAsr`；
  装好的安装包体积会从约 61 MB 涨到约 139 MB（sidecar 里多了 ctranslate2 / onnxruntime / PyAV 的原生库）。
  安装版里没有 Python，界面上那句"pip install"只适用于源码运行
- 模型始终不随包：首次分析时从 huggingface.co 下载到数据目录的 `models\`（默认 small 档约 480 MB）
- 字幕使用系统字体（微软雅黑 / Noto Sans SC 等）

### Q: 首次分析很慢？
A: 若安装了 faster-whisper，第一次会下载模型并加载到内存；之后的分析复用进程内缓存。
未安装 faster-whisper 时分析只做音量/VAD 静音检测，通常几秒完成。

### Q: 语音识别模型下载失败？
A: 模型来自 huggingface.co，网络不可达时把 `ACV_ASR_HF_ENDPOINT` 设为镜像（例如
`https://hf-mirror.com`）后重新分析；报错信息里也会给出同样的提示。模型缓存在数据目录的
`models\` 下，下载成功后不再依赖网络。

### Q: 在 Anaconda 环境里识别时进程直接退出？
A: 常见于多个 OpenMP 运行时冲突（`OMP: Error #15`）。后端会自动设置
`KMP_DUPLICATE_LIB_OK=TRUE` 规避；若仍复现，请改用虚拟环境安装 `.[asr]`。

## 使用相关

### Q: 支持哪些视频格式？
A: 导入按扩展名白名单校验：`.mp4 .mov .mkv .avi .webm .m4v .mts .m2ts`。
导出为 MP4，字幕产物为 `.ass` 或 `.srt`。

### Q: 支持可变帧率（VFR）视频？
A: 会探测并在导入面板提示。装了 ffprobe 时按 `r_frame_rate` 与 `avg_frame_rate` 判定；
只有 ffmpeg 时（安装包就是这种情况）改用 `ffmpeg -i` 输出里的 `fps` 与 `tbr` 差异判定，
属启发式：打印舍入差（29.97/30）不会误报，但极端封装下可能漏判。
剪切按微秒时间戳重编码，因此不会直接沿用源的关键帧，
但源时间基不规整时，边界帧可能与预览有细微差异。

### Q: 中文识别准确率不高？
A: 目前只有 faster-whisper 一个引擎，可提高模型档位（tiny / base / small / medium / large）
或在设置里改用别的 LLM 端点。未安装该依赖时不会有转录，字幕功能会提示「尚无语音识别结果」。

### Q: 提示「LLM 不可用，已按规则策略」是什么意思？
A: 剪辑建议改由本地规则给出（静音超过阈值即建议删除）。这是可用但较保守的策略，
配置好 LLM 端点后重新分析即可获得语义级建议。

### Q: 导出提示「尚无语音识别结果，无法生成字幕」？
A: 该导出选择了烧录字幕或独立字幕文件，但项目没有转录结果。
改用「无字幕」导出，或先安装并运行语音识别。

### Q: LLM 剪辑建议不准确？
A: 建议不会自动生效，必须逐条确认；也可以手工增删剪切区间。撤销/重做保留完整版本历史。

### Q: 渲染速度慢？
A: 预览走 720p 快速验证并带方案级缓存（改动剪辑方案才会重渲染）。
导出是单趟 `-filter_complex`，主要瓶颈在编码，关闭其他高 CPU 程序与使用 SSD 有帮助。

### Q: 导出后画面方向不对，或在某些播放器里横倒？
A: 手机竖拍素材通常把像素按横向存储、再用元数据标注旋转。渲染时我们会把探测到的角度
真正转进像素，并且**不**在成片里保留旋转元数据，因此不依赖播放器是否识别该标记。
本版本之前导入的项目里方向记录可能是 0，重新绑定一次源文件（或重新导入）即可修正。

### Q: 字幕显示乱码？
A: 字体名会被白名单校验，白名单外的名称自动替换为微软雅黑；请确认系统已安装该字体。
SRT 不含样式信息，乱码通常来自播放器缺字体而非导出本身。

### Q: 导出的 .otio 在 Premiere / Resolve / Kdenlive 里显示媒体离线？
A: `.otio` 只保存剪辑决策（片段顺序与起止）和指向源素材的 `file://` 路径，不含媒体本身。
换机器或移动过源文件后，需要把素材放回同一路径，或用 OTIO 的 media linker / 软件的重连功能指回新路径。
本项目产物的结构由官方 `opentimelineio` 读取器在测试里验证（`tests/e2e/test_otio_reader.py`）。

## 数字人相关

### Q: 数字人合成功能怎么用？
A: 当前版本**未接入** HeyGen / D-ID 的真实 API，页面会显示「功能未启用」且合成按钮不可用。
剪辑、字幕与导出链路不受影响。

## 数据与安全相关

### Q: 项目数据存储在哪里？
A: `%LOCALAPPDATA%\auto_clip_video\`：项目索引 `app.db`、每个项目自己的 SQLite（EDL 修订与识别结果）、
预览缓存、日志。未指定输出路径时导出到同目录的 `exports\`。源视频只记录绝对路径，不会被复制进数据目录。

### Q: API Key 安全吗？
A: 保存在 Windows 凭据管理器，不写入数据库、配置文件或日志；日志写盘前会遮蔽密钥、令牌与用户名。
本机 API 只监听环回口，并且每个进程有一枚随机令牌，同时校验 Host / Origin 头。

### Q: 凭据管理器不可用会怎样？
A: 后端会降级为「仅当前进程内存」保存密钥，设置页会明确提示重启即失效。

### Q: 如何备份项目？
A: 复制 `%LOCALAPPDATA%\auto_clip_video\` 整个目录。API Key 存在凭据管理器里，不随目录迁移。
