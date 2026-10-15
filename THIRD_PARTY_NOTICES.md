# Third Party Notices

This product includes software developed by the following third parties.

## Python Dependencies

随 sidecar 可执行文件一起分发的运行时依赖（许可证取自各包的 PEP 639 元数据）。

| Package | Version | License |
|---------|---------|---------|
| fastapi | >=0.110 | MIT |
| uvicorn | >=0.27 | BSD-3-Clause |
| pydantic | >=2.6 | MIT |
| pydantic-settings | >=2.2 | MIT |
| sqlalchemy | >=2.0 | MIT |
| greenlet | 随 sqlalchemy[asyncio] | MIT AND PSF-2.0 |
| aiosqlite | >=0.20 | MIT |
| structlog | >=24.1 | MIT OR Apache-2.0 |
| python-multipart | >=0.0.9 | Apache-2.0 |
| websockets | >=12 | BSD-3-Clause |
| httpx | >=0.27 | BSD-3-Clause |
| keyring | >=25 | MIT |

pytest、ruff、mypy 等只在开发环境使用，不进入分发产物。
`opentimelineio`（Apache-2.0，含 C++ 扩展）同样只在开发环境使用：它只是 OTIO 导出产物的
验证工具（`tests/e2e/test_otio_reader.py`），运行时导出完全由标准库完成，因此不会被打进 sidecar。

## Optional Dependencies

| Package | Version | License |
|---------|---------|---------|
| faster-whisper | >=1.0 | MIT |
| ctranslate2 | >=4.4,<4.5 | MIT |
| silero-vad | >=5.1 | MIT |
| imageio-ffmpeg | >=0.5 | BSD-3-Clause |

## Frontend Dependencies

| Package | Version | License |
|---------|---------|---------|
| react | 18.x | MIT |
| vite | 5.x | BSD-3-Clause |
| typescript | 5.x | Apache-2.0 |
| zustand | 4.x | MIT |
| antd | 5.x | MIT |

## Tauri Dependencies

| Package | Version | License |
|---------|---------|---------|
| tauri | 2.x | Apache-2.0 / MIT |
| windows-sys | 0.61 | MIT / Apache-2.0 |

## Bundled Binaries

| Binary | Version | License |
|--------|---------|---------|
| FFmpeg | 6.0+ | LGPL-2.1+ (configurable) |
| ffprobe | 6.0+ | LGPL-2.1+ |

## Bundled Fonts

| Font | License |
|------|---------|
| Noto Sans CJK SC | OFL-1.1 |
| Source Han Sans (思源黑体) | OFL-1.1 |