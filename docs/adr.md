# Architecture Decision Records (ADR)

## ADR-001: 静音检测方法选择

**日期**: 2026-10-12
**状态**: POC 验证后决定
**背景**: 需要从 auto-editor、Silero VAD、LLM candidate-EDL 三种方法中选择默认静音检测方法。
**决策**: 待 POC-A 验证后确定。预期推荐 Silero VAD（F1 >= 0.80）。
**后果**: 
- 若 Silero VAD 通过，设为默认；LLM 作为增强。
- 若未通过，启动备选方案评估。

---

## ADR-002: 字幕渲染引擎选择

**日期**: 2026-10-12
**状态**: 已决定
**背景**: 需要选择字幕渲染引擎，支持中文全角标点、1080p 渲染。
**决策**: 使用 FFmpeg + libass 作为字幕渲染引擎。
**理由**: 
- libass 原生支持 ASS 格式，渲染质量高。
- FFmpeg 已是项目核心依赖，无需额外引入。
- 通过白名单机制确保安全区域和样式安全。
**后果**: 需打包中文字体（Noto Sans CJK SC）避免乱码。

---

## ADR-003: OTIO 导出用官方读取器验证后放开

**日期**: 2026-10-12，2026-10-13 修订
**状态**: 已决定（取代前一版"长期挂 feature flag"的结论）
**背景**: 转换器早已存在，但产物只有"自己写的 JSON 自己读"的自检，官方
`opentimelineio` 读取器实际会报 `expected type TimeRange ... found AnyDictionary`，
即当时给外部工具的是一份打不开的文件，端点因此只能返回 501。
**决策**: 按官方 0.18.1 自己序列化的结果重写转换器（`Timeline.1 → Stack.1 → Track.1(Video) →
Clip.2`，`RationalTime` 以微秒为时间基，`target_url` 用 `file://` URL），运行时仍只用标准库；
用官方读取器写一组 e2e 测试（`tests/e2e/test_otio_reader.py`）作为放开端点的唯一依据。
**理由**: 交换格式的正确性不由本项目定义，只有官方读取器接受才算数；把 opentimelineio
装成运行时依赖会把 C++ 扩展带进 PyInstaller 产物，而为生成一份 JSON 不值得。
**后果**: `GET /projects/{project_id}/otio` 真正产出文件，`capabilities.otio_available=true`；
feature flag 与 `otio_enabled()` 删除。升级 opentimelineio 版本时那组 e2e 测试就是回归门禁，
它没跑过（未安装该包）时应视为"未验证"而不是"通过"。

---

## ADR-004: 数字人服务与主剪辑链路解耦

**日期**: 2026-10-12
**状态**: 已决定
**背景**: 数字人合成依赖外部 API（HeyGen/D-ID），可能不可用。
**决策**: AvatarService 与主剪辑链路完全解耦，数字人不可用时主剪辑功能不受影响。
**理由**: 保证核心功能可靠性，数字人作为可选增强功能。
**后果**: AvatarService 异常时返回 error_message，不抛出异常中断主流程。

---

## ADR-005: 时间单位统一为整数微秒

**日期**: 2026-10-12
**状态**: 已决定
**背景**: 视频编辑需要精确时间表示，浮点数有精度问题。
**决策**: 全系统时间单位统一为整数微秒 `[start_us, end_us)`，左闭右开。
**理由**: 
- 整数微秒精度足够（1us = 10^-6s）。
- 避免浮点数比较误差。
- 左闭右开区间便于拼接和计算。
**后果**: 所有模型字段使用 `int` 类型，后缀 `_us`。

---

## ADR-006: CredentialStore 使用 Windows Credential Manager

**日期**: 2026-10-12
**状态**: 已决定
**背景**: API Key 等敏感信息需要安全存储。
**决策**: 使用 Windows Credential Manager（通过 keyring 库）存储敏感凭证。
**理由**: 
- 操作系统级加密保护。
- 不写入 SQLite/配置文件/日志。
- 符合 spec R-S01 安全要求。
**后果**: 测试中使用 `use_in_memory=True` 避免 keyring 全局状态污染。