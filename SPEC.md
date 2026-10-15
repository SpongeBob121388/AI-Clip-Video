# Windows AI 口播剪辑与个人数字人软件
## 第二轮视频剪辑相关开源项目调研报告

**版本：** Research v1.1（补充报告）  
**调研核对日期：** 2026-10-11  
**用途：** 为开发实施 Spec v1.1 提供有证据的候选比较和架构决策。  
**范围：** 聚焦视频剪辑、EDL/时间线、自动静音剪切、字幕和音视频渲染；不重新评估整个数字人/TTS API 市场。  
**重要限制：** 本报告是工程选型研究，不是法律意见，不代表所有项目都已在目标 Windows 机器上实际编译或 benchmark。GitHub 官方页面可验证仓库功能、许可证声明、构建提示和项目状态；Windows 兼容、中文质量和真实用户素材表现仍需按 Spec 执行 POC。

## 1. 执行摘要

本轮研究围绕一个核心问题展开：能否通过整合现有开源项目，减少我们对真人口播自动剪辑模块的自研成本，同时保留可控的剪辑逻辑、低置信度安全规则、项目持久化、中文字幕和 Windows 部署质量？

**推荐结论：保持轻量化的专用剪辑架构，不嵌入完整的非线性编辑器（NLE）。** MVP 使用自有 EDL（Edit Decision List）/SQLite 项目数据模型作为“编辑真相”，由 FFmpeg/ffprobe 负责媒体探测和确定性渲染。`video-use` 可以提供 AI 删剪、EDL 与 FFmpeg 渲染的参考实现；`auto-editor` 值得做对照实验，验证音量/静音阈值剪切，但不应直接作为生产依赖；OpenTimelineIO（OTIO）适合未来与专业编辑器交换时间线，不适合作为内部业务状态或渲染引擎。Subtitle Edit 是很好的外部字幕 QA/手动检查工具，但不是需要嵌入的运行时库。

不推荐在 MVP 中嵌入 Kdenlive、Shotcut、OpenShot 或 Olive。它们是成熟度、规模和许可属性各异的完整 NLE，不是可以轻松嵌入 UI 的轻量剪辑控件。将完整编辑器放入应用会引入大型 UI/渲染栈、复杂工程文件格式、额外的打包兼容问题和许可证审计工作，超出当前“文本驱动删剪、人工确认、简单字幕与画幅”的需求范围。

### 推荐矩阵

| 项目 | 功能匹配 | 技术集成价值 | 许可/供应链风险 | 决策 |
|---|---:|---:|---:|---|
| FFmpeg/ffprobe | 高 | 高 | 中，取决于编译选项和打包方式 | P0 采用 |
| 自有 JSON EDL + SQLite | 高 | 高 | 低 | P0 必须 |
| browser-use/video-use | 高 | 中-高 | 中，需审计复制代码并替换默认服务 | 参考/选择性复用 |
| auto-editor | 中-高（静音剪切） | 中 | 需要检查其 source vs release binary/licensing | 隔离 POC，对照基准 |
| OpenTimelineIO | 中（时间线交换） | P1/P2 有价值 | 低-中；适配器需分开锁定 | P1/P2 选配导出 |
| Subtitle Edit | 中（字幕校验） | 作为独立应用有价值 | MIT 主仓库，发行/依赖仍要审查 | 外部 QA，不嵌入 |
| PyAV | 中（媒体时间戳诊断） | 针对复杂时基有价值 | BSD-3-Clause，但 wheel 依赖/FFmpeg 组件需登记 | 按需 POC |
| PySceneDetect | 低（MVP 单人连续口播） | P2 可能有价值 | BSD-3-Clause | P2 |
| Kdenlive/MLT | 当前需求下低 | 外部工程互通可参考 | Kdenlive GPL-3.0；MLT 与具体组件版本须审查 | 不嵌入，P2 外部 NLE 导出 |
| Shotcut | 当前需求下低 | 外部 NLE | GPL-3.0，Qt/MLT/FFmpeg/Frei0r 等依赖 | 不嵌入 |
| OpenShot/libopenshot | 中（通用剪辑引擎） | 中-低 | libopenshot LGPLv3/商业许可并不覆盖全部依赖 | MVP 不采用 |
| LosslessCut | 低（关键帧无损裁剪） | 低 | GPL-2.0 | 不集成 |
| Olive | 当前需求下低 | 低 | GPL-3.0，项目自述 alpha 且 highly unstable | 不采用 |
| MoviePy | 中 | 低，现有需求可直接调用 FFmpeg | MIT，但新增抽象/调试面 | MVP 不引入 |
| Remotion | 复杂动态图层时有价值 | MVP 较低 | 需核验按 render/企业许可；Automators 有官方计价 | 不采用 MVP |

## 2. 方法与证据等级

本报告优先使用官方 GitHub 仓库、官方许可证页面、项目文档与 release notes。每项结论分为：

- **已由官方页面核对**：项目简介、仓库声明的代码许可证、公开功能、文档说明、可见 release note/构建要求。
- **需本地 POC**：Windows 11 打包、中文字体、边界精准度、VFR 媒体、性能、内存、输出质量和最终供应链。
- **需法律/服务条款复核**：分发完整应用、动态/静态链接、外部二进制、模型权重、第三方插件与字体等。

“开源许可证”不能单独证明可在任意方式下集成。项目代码、预编译二进制、依赖、模型权重、字体、外部 API 的服务条款分别审计。尤其需要避免从主仓库的许可证推断它所调用的 FFmpeg build、模型权重或 npm/Python 依赖也具有相同许可。

## 3. 项目详细评估

### 3.1 browser-use/video-use

**官方仓库：** https://github.com/browser-use/video-use  
**仓库显示许可证：** MIT。页面说明其目标是让 coding agents 操作视频编辑工作流，包括删 filler words/dead space、FFmpeg 处理、字幕/叠加等。引用：[官方仓库](https://github.com/browser-use/video-use)。

**与我们匹配的部分：**
- 能参考从原素材、转写/时间戳到 cut decisions/EDL 的自动化流程；
- `helpers/` 下的渲染和分析代码可用于理解如何组织 FFmpeg 工作流；
- 它把“LLM 做编辑判断”和“FFmpeg 执行实际剪辑”分层，符合我们产品方向。

**限制：**
- 它以 coding agent + 文件目录为界面，不提供完整 Windows 桌面应用的工程持久化、文本编辑、撤销历史和费用账本；
- 默认使用的 ASR/API/模型交互不是产品不可替换的业务合同；
- Windows 中文路径、中文字体、纵向视频旋转 metadata、渲染失败恢复仍要单独测试；
- 代码 MIT 并不自动解决依赖和外部 API 的费用、条款、素材处理方式。

**建议：** 只借鉴或逐文件复用经过审查的 EDL/FFmpeg helper。锁定具体 commit 后，逐文件记录来源。保持 ASR/LLM Provider 可替换；不要整体复制其 agent workflow 当作桌面应用的后台业务层。

### 3.2 auto-editor

**官方仓库：** https://github.com/WyattBlue/auto-editor  
**功能：** 官方 README 将其定义为分析音量等信号后进行自动视频/音频编辑的命令行应用；支持静音/活动标签、margin、运动/音频阈值，并能将编辑结果输出为 Premiere、Resolve、Final Cut Pro、Shotcut、Kdenlive 等格式。[官方 README](https://github.com/WyattBlue/auto-editor/blob/master/README.md)。

**许可证和分发风险：** 官方 README 说明仓库代码按 Public Domain/Unlicense，且 release binaries 可能含不同开源许可证。官方 release notes 还曾引入与高分辨率/多源 timeline export 相关的 license-key 约束。[Releases](https://github.com/WyattBlue/auto-editor/releases)。这意味着需要区分源码、发布的 CLI/GUI 二进制和附加 assets，不能因为源码 Unlicense 就将任意预编译包作为无条件免费的生产组件。

**用途判断：** 它适合做静音/低活动段切除的对照工具，不能决定语义性删剪，也无法知道一句低音量话是否包含关键内容。我们的 LLM 需要通过文本片段 ID 给出建议，低置信度内容要保留/待审。Auto-Editor 可以帮忙回答“纯音量阈值能做到怎样的基线”，不能作为最终剪辑决策层。

**建议：** P0 隔离 benchmark，不被运行时依赖，不将发行版二进制打包进安装器。只有在对照测试显示有显著质量/开发价值且许可证复核完成后，才重新提交 ADR。

### 3.3 OpenTimelineIO（OTIO）

**官方仓库：** https://github.com/AcademySoftwareFoundation/OpenTimelineIO  
**代码许可证：** Apache-2.0。官方将其描述为编辑时间线的 API 和 interchange format，包含剪辑顺序、长度、引用的媒体，而不是实际的媒体容器或渲染器。[官方仓库](https://github.com/AcademySoftwareFoundation/OpenTimelineIO)。

**优势：**
- 比私有 JSON EDL 更有利于以后和多种 NLE 交换时间线；
- 时间线对象与 media references 能够表达 clips、gaps 等编辑结构；
- Python API 方便在后端构建 export adapter。

**限制：**
- OTIO 不处理视频编码/渲染，仍需 FFmpeg/其它 renderer；
- 最近版本的 release notes 提及将多数 legacy adapters 移至独立版本化的插件包，因此不能假设 `pip install opentimelineio` 就含所有导入导出适配器。[Releases](https://github.com/AcademySoftwareFoundation/OpenTimelineIO/releases)；
- 与我们 UI 交互所需的 review status、LLM confidence、transcript revision、undo history、cost records 不是 OTIO 的主要职责。

**建议：** 内部仍用自己的 JSON Schema + SQLite 项目状态。P1/P2 增加 OTIO export POC，验证 clip order 和 duration，不承诺能 round-trip 复刻所有 Kdenlive/Shotcut effects。

### 3.4 Kdenlive 与 MLT

**Kdenlive：** https://github.com/KDE/kdenlive，官方仓库显示 GPL-3.0。Kdenlive 的项目文件 `.kdenlive` 基于 MLT XML，用于描述媒体引用与 timeline；官方说明 MLT 能渲染 Kdenlive 工程，同时 Kdenlive 特有 metadata 提供额外 UI/项目功能。[格式文档](https://github.com/KDE/kdenlive/blob/master/dev-docs/fileformat.md)。

**MLT：** https://github.com/mltframework/mlt，官方项目自述为 LGPL 多媒体编辑框架。[官方仓库](https://github.com/mltframework/mlt)。

**潜在价值：** MLT 本身是编辑/渲染框架；Kdenlive 工程格式可以作为未来外部编辑器互通候选。若以后需要多轨时间线、音视频混合和复杂效果，可评估 MLT 是否比直接构建渲染器更合适。

**为什么 MVP 不集成：** 我们当前不需要多轨编辑；要正确生成 Kdenlive XML，需要处理版本、profile、producer/playlist/tractor、clip references、property namespaces 与 Kdenlive 元数据。直接写项目文件可能出现“能被 MLT 渲染但打开后编辑状态不完整”的差异。嵌入 Kdenlive UI 会引入 GPL 应用、Qt/KDE/MLT 依赖和额外打包成本。

**建议：** MVP 保持 FFmpeg；将“生成可在 Kdenlive 打开的工程”放在 P2，以 export POC 验证。对 MLT 直接链接/打包的法律和技术路径另做 ADR，不先引入。

### 3.5 Shotcut

**官方仓库：** https://github.com/mltframework/shotcut。官方 README 显示 GPLv3，并列 Qt 6、MLT、FFmpeg、Frei0r、SDL 等直接依赖；项目也警告从源码构建适合熟悉其 build setup 的 beta testers/contributors。[官方 README](https://github.com/mltframework/shotcut/blob/master/README.md)。

Shotcut 很适合作为“用户需要手动进行复杂精剪时，可以外部打开”的备选 NLE，但不应被当成一个可轻易嵌入的编辑组件。MVP 的需求是文本驱动修改，而不是多轨 timeline。若未来提供外部打开功能，优先输出标准媒体与 OTIO/可验证 interchange，而不是改造 Shotcut 源码。

### 3.6 OpenShot / libopenshot

- GUI 仓库：https://github.com/OpenShot/openshot-qt
- C++ 编辑库说明：https://www.openshot.org/libopenshot/

OpenShot README 显示其需要 libopenshot、libopenshot-audio、FFmpeg/Libav 与 Qt binding 等。官方 libopenshot 页面说明其采用 LGPLv3 与商业许可双轨，且商业许可不覆盖 JUCE 或其它依赖库。它确实是比“只调用 FFmpeg”更完整的编辑引擎，但带来了 native C++ build、Qt、media library 版本和额外许可证的组合成本。

**建议：** 当前 1080p、单源删剪、字幕和画幅的工作流没有必要引入整个 libopenshot。若日后需要多轨 timeline engine，再做独立 POC；不要因“可调用的 C++ 库”就忽略构建/依赖/许可复杂度。

### 3.7 LosslessCut

**官方仓库：** https://github.com/mifi/lossless-cut，仓库标注 GPL-2.0。[官方仓库](https://github.com/mifi/lossless-cut)。其定位是针对音视频的无损快速切割工具。无损剪切受关键帧和流 copy 限制；对话口播中的词级时间点通常不保证正好落在 keyframe 上，而且我们的成片需要做字幕、缩放和模糊填充，最终通常要重新编码。

**建议：** 不复用其主应用。它可作为人工快速剪切工具的外部参考，但与内部 EDL + 精准 renderer 的核心并不相同。

### 3.8 Subtitle Edit

**官方仓库：** https://github.com/SubtitleEdit/subtitleedit。仓库标记 MIT；README 有 Windows 系统要求和自动构建说明。[官方仓库](https://github.com/SubtitleEdit/subtitleedit)。

**价值：** 作为独立应用验证 SRT/ASS 文件、修正时间轴、检查字幕格式是实用的；但它不是底层字幕 renderer。我们的 UI 只要求基础字幕样式调整，FFmpeg/libass 处理最终硬字幕即可。

**建议：** 开发/QA 期间把 Subtitle Edit 作为可选外部 QA 工具，不把它嵌进产品，也不在运行时依赖它安装。

### 3.9 PyAV

**官方仓库：** https://github.com/PyAV-Org/PyAV。仓库标记 BSD-3-Clause，并说明可直接访问 container/stream/packet/codec/frame；目前官方文档提供 Windows wheels，近版要求 Python 3.12+ 且 wheel 包含 FFmpeg 相关动态库。[官方 README](https://github.com/PyAV-Org/PyAV)、[安装说明](https://github.com/PyAV-Org/PyAV/blob/master/docs/overview/installation.rst)。

PyAV 是需要精确访问 PTS/time_base、检查 decoded frames 或分析 VFR 视频时有价值的工具。如果只是 probe、cut、concat 和 render，直接使用 FFmpeg CLI 更容易打包/诊断。因此它应由具体需求触发，而不是成为默认依赖。若引入，必须把它 wheel 中附带的 FFmpeg 二进制/库也纳入 SBOM/许可证清单，并避免两个不同 FFmpeg build 在 PATH 中产生冲突。

### 3.10 MoviePy

**官方仓库：** https://github.com/Zulko/moviepy，代码仓库标记 MIT。[官方仓库](https://github.com/Zulko/moviepy)。它对快速 Python 脚本方便，但当前需求不需要复杂的 Python frame-by-frame 合成。使用 MoviePy 不能自动解决时间戳/时基/关键帧/字幕字体问题，还会增加 Python wrapper 的版本和错误排查面。

**建议：** MVP 不引入；仅当有一个独立功能能显著缩短开发且不存在可直接使用的 FFmpeg filter 时重新评估。

### 3.11 PySceneDetect

**官方仓库：** https://github.com/Breakthrough/PySceneDetect。页面标记 BSD-3-Clause，并展示 v0.7.1（2026-07-21）的 release。[官方仓库](https://github.com/Breakthrough/PySceneDetect)。这是成熟的 scene cut/transition detector，可输出场景时间点或配合 FFmpeg 分割片段。

我们的主视频是单人 5–15 分钟连续口播。是否发生画面切换不等于某句话是否应该删，故 scene detection 不是 P0。P2 若加入 B-roll 或多镜头素材筛选，再纳入 benchmark。

### 3.12 Olive

**官方仓库：** https://github.com/olive-editor/olive。仓库标 GPL-3.0，明确写明 alpha 且 highly unstable，并提供 alpha/unstable development builds。[官方仓库](https://github.com/olive-editor/olive)。

**建议：** 不作为产品代码依赖，也不基于其项目格式进行互通；最多作 UI 研究参考。

### 3.13 Remotion

**官方许可与定价：** https://www.remotion.dev/docs/license/pricing。Remotion 适于使用 React 编写程序化视频/动画，但当前官方许可是按个人/小团队与更大组织区分；Automators 方案存在按 render 收费及最低月费，必须检查项目身份、使用方式与当日条款。[官方定价/许可](https://www.remotion.dev/docs/license/pricing)。

**建议：** MVP 主要字幕和画幅工作用 FFmpeg ASS/libass 更直接。只有未来需要复杂动态字幕模板或图形动画时，才对 Remotion 建立独立 POC 和预算评估。

## 4. 开源拼装方案的关键设计原则

### 4.1 分离“编辑判断”与“执行渲染”

- ASR/对齐输出 utterance/word spans；
- VAD/音量检测输出静音候选；
- 云 LLM 基于带上下文的候选 ID 给出删除/保留建议；
- 应用层统一校验并让人工确认不确定项；
- 自有 EDL 计算源范围与输出时间映射；
- FFmpeg 执行切片、字幕、画幅和编码。

这让 ASR、LLM、VAD 和渲染器可以独立替换，避免项目锁定在一个脚本或单个服务的默认用法中。

### 4.2 时间线内部模型要比一组 float start/end 更严格

建议所有计算采用整数微秒区间 `[start_us, end_us)`，再结合 ffprobe `time_base`/PTS 验证源媒体时间。输出时间线由接受的 cuts 的 complement 确定性计算；源时间必须单调、顺序不变。渲染后再用 ffprobe 检查时长、分辨率、音视频流和 codec。

对于 Variable Frame Rate（VFR）、非零 `start_time`、手机视频旋转 metadata，必须建立黄金测试样例。若无法确认准确映射，宁可要求用户检查/微调边界，也不能静默输出错误切点。

### 4.3 字幕渲染优先 ASS/libass + FFmpeg

为第一版基础样式使用 ASS 样式可支持字体、字号、颜色、位置、描边和边距控制；用户可以在 UI 里编辑白名单样式字段，系统生成 ASS，不接受任意用户输入直接拼进 FFmpeg filter 字符串。必须测试中文字体存在性、字体回退、字幕 safe margin、全角标点、路径转义和竖屏安全区域。字幕编辑不应改变原始音频轨道。

### 4.4 预览和最终导出必须共用同一份 RenderPlan

预览可降分辨率/压缩质量，但片段选择、源/输出时标映射、字幕文案、画幅变换和字体配置应与最终导出共享同一份结构化 RenderPlan。不要分别维护一套“预览逻辑”和一套“导出逻辑”，否则 UI 看起来正确、最终视频却发生错位。

## 5. 建议的接口和数据边界

### 5.1 EDL 范例

```json
{
  "schemaVersion": 1,
  "projectId": "uuid",
  "source": {
    "uri": "project-relative/source.mp4",
    "durationUs": 612340000,
    "timeBase": {"num": 1, "den": 90000},
    "isVariableFrameRate": false
  },
  "suggestions": [
    {
      "id": "cut-001",
      "sourceStartUs": 12400000,
      "sourceEndUs": 15100000,
      "wordIds": ["w-35", "w-36"],
      "reason": "false_start",
      "status": "pending",
      "strength": "standard",
      "explanation": "句子重启前的无效起头；不确定时保留"
    }
  ],
  "output": {"aspectRatio": "9:16", "resolution": "1080x1920", "subtitlePresetId": "default-cn"}
}
```

上述 schema 用于说明结构，不应视为最终 JSON Schema；真正实现时必须加类型验证、迁移策略和更多元数据。

### 5.2 Renderer Contract

`render(project_revision, render_preset) -> RenderJob` 不直接接收任意 FFmpeg argv。后端从已验证的剪辑区间生成 RenderPlan，再按安全函数构造 FFmpeg argv；所有文件路径、字幕字段与滤镜值都必须经过校验。渲染失败要保留工程和诊断日志；成功必须 `ffprobe` 验证产物。

### 5.3 Undo/Redo 与项目持久化

建议采用以 command/patch 为中心的编辑历史，至少保存每次 accepted/rejected cut、transcript revision、subtitle change、aspect-ratio change 和 render preset change。MVP 不需要复杂实时协同 CRDT；事务提交后落入 SQLite。每次渲染保存使用的 project revision 和 RenderPlan hash，以便能解释“这个文件由哪个版本的工程生成”。

## 6. POC 计划（新增）

| POC | 内容 | 测试素材 | 判定标准 |
|---|---|---|---|
| POC-A | `auto-editor` vs Silero VAD vs LLM candidate-EDL | 10 段普通话口播，含轻声、停顿、重复、背景噪声、专有词 | 统计误删关键内容、漏掉无效片段、边界误差、耗时；结果用于确认 auto-editor 是否仅留实验工具 |
| POC-B | FFmpeg 1080p 输出和中文字幕 | MP4/MOV、横/竖视频、旋转 metadata、中文路径、空格路径、VFR/可变时基样本 | 产物能播放，字幕无缺字/方框字，分辨率/时长/音频流符合 preset，错误有明确提示 |
| POC-C | OTIO 导出小项目 | 20–40 段 cut 的 EDL | OTIO reader round-trip 保留 clip order 与 duration；如果 adapter 缺失则明确记录插件依赖和推迟决定 |
| POC-D | Kdenlive external-project export（P2 评估，不挡 MVP） | 仅含 source clip、simple cuts、no effects 的测试时间线 | 由 Kdenlive 打开并渲染；不能把“XML 能生成”当作成功，需在目标版本 GUI 中检查 |
| POC-E | Windows package reproducibility | 新建 Windows 11 VM、无开发环境 | 安装后可导入/剪辑/渲染/重新打开项目；FFmpeg/字体/sidecar 路径正确；Third-party notices 可追溯 |

## 7. 新增风险与避免方案

1. **License mismatch：** 仓库代码许可、release binary、依赖和字体不一致。缓解：SBOM + `THIRD_PARTY_NOTICES.md` + build flags + 每次 release license gate。
2. **自动静音误删内容：** 音量阈值可能删掉轻声讲话，语义模型也会误判。缓解：阈值只产生候选；低置信度保留；渲染前预览/确认。
3. **VFR/PTS 偏移：** 毫秒浮点转整数或手机旋转/起始 PTS 处理错误，会导致切点和字幕偏移。缓解：整数 microseconds + source time_base/PTS + 黄金样例。
4. **字幕一致性：** UI 预览和 FFmpeg burn-in 字体/换行不同。缓解：共用 RenderPlan 和 ASS 生成器；同一 FFmpeg build 做 golden screenshots。
5. **将 NLE 误认成 SDK：** 完整应用可能没有可稳定集成的轻量 API，许可与依赖也很复杂。缓解：MVP 不嵌入；先建立自有简单模型，未来经 ADR 增量做 exchange export。
6. **供应商或工具锁定：** EDL 与某个项目的私有字段/默认 API 绑定。缓解：自有 schema + Provider Adapters；OTIO 作为交换格式而非核心状态。

## 8. 明确不推荐清单

- 不将 Kdenlive、Shotcut、OpenShot、Olive 整体嵌入 MVP。
- 不将 `auto-editor` 当成 LLM 语义剪辑引擎；不在许可证/发行限制核实前打包 release binary。
- 不把 LosslessCut 的无损关键帧剪切作为精确词级剪辑 renderer。
- 不为了 Python API 顺手就叠加 MoviePy；优先 FFmpeg CLI，只有额外能力明确必要时才引入 PyAV。
- 不把 OTIO 当作渲染器或完整项目状态数据库。
- 不在 MVP 引入 PySceneDetect 作为删剪决策主引擎；场景切换和语义无效内容是不同问题。
- 不把 Remotion 用作基础中文字幕渲染；其许可/automator 计费和浏览器渲染复杂度当前没有收益。
- 不未经审核复制项目代码或模型文件；对代码、模型权重、字体、FFmpeg build 和 API 服务分别做供应链审查。

## 9. 最终架构建议

**MVP 主链路：**

`Tauri/React UI → 本机 FastAPI orchestrator → ASR Provider → 云 LLM candidate decisions → own EDL + SQLite project state → FFmpeg/ffprobe RenderPlan → MP4 1080p`

数字人链路仍由独立 Avatar Provider Adapter 处理，完成的数字人片段进入同一个 RenderPlan/字幕/画幅/导出管线。

**可集成清单：**
- P0：FFmpeg/ffprobe；从 `video-use` 挑选经过 license/commit 审核的有价值 helper 或仅参考流程；可用 `faster-whisper`/WhisperX/Qwen ASR 等独立 Provider 做 POC（由整体 Spec 的 ASR Gate 选择）。
- POC only：`auto-editor` 作为静音删剪对照；`PyAV` 仅在需要精细 PTS 操作时；OTIO 时间线导出测试。
- P1/P2：OTIO export、外部 Subtitle Edit QA 入口、Kdenlive/Shotcut interchange、PySceneDetect/B-roll。
- 不进 MVP：完整 NLE embed、Remotion render stack、MoviePy、LosslessCut 代码、Olive。

## 10. 参考资料（官方来源）

调研日期：2026-10-11。以下均应在依赖锁定时重新核查。

1. browser-use/video-use README/license — https://github.com/browser-use/video-use
2. WyattBlue/auto-editor README — https://github.com/WyattBlue/auto-editor/blob/master/README.md
3. WyattBlue/auto-editor Releases — https://github.com/WyattBlue/auto-editor/releases
4. AcademySoftwareFoundation/OpenTimelineIO — https://github.com/AcademySoftwareFoundation/OpenTimelineIO
5. OTIO release notes — https://github.com/AcademySoftwareFoundation/OpenTimelineIO/releases
6. Kdenlive repo — https://github.com/KDE/kdenlive
7. Kdenlive project file format — https://github.com/KDE/kdenlive/blob/master/dev-docs/fileformat.md
8. MLT Framework — https://github.com/mltframework/mlt
9. Shotcut README — https://github.com/mltframework/shotcut/blob/master/README.md
10. OpenShot Qt app — https://github.com/OpenShot/openshot-qt
11. libopenshot licensing — https://www.openshot.org/libopenshot/
12. LosslessCut — https://github.com/mifi/lossless-cut
13. Subtitle Edit — https://github.com/SubtitleEdit/subtitleedit
14. PyAV — https://github.com/PyAV-Org/PyAV
15. PyAV installation/build instructions — https://github.com/PyAV-Org/PyAV/blob/master/docs/overview/installation.rst
16. PySceneDetect — https://github.com/Breakthrough/PySceneDetect
17. MoviePy — https://github.com/Zulko/moviepy
18. Olive — https://github.com/olive-editor/olive
19. Remotion License/Pricing — https://www.remotion.dev/docs/license/pricing
20. FFmpeg license — https://github.com/FFmpeg/FFmpeg/blob/master/LICENSE.md
21. FFmpeg filter documentation — https://ffmpeg.org/ffmpeg-filters.html
