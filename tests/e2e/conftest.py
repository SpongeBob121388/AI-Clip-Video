"""端到端测试共用夹具：真实 FFmpeg 可执行文件与合成测试片。

这些用例不调用任何 mock —— 只有真跑一遍才能证明"链路接通"而不只是"函数存在"。
环境里没有 FFmpeg 时直接 skip，不会伪装成通过。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SOURCE_DURATION_US = 8_000_000


def _find_ffmpeg() -> str | None:
    hint = os.environ.get("ACV_FFMPEG_PATH")
    for candidate in (hint, "ffmpeg"):
        if candidate:
            found = shutil.which(candidate)
            if found:
                return found
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - 依赖缺失属于常见情况
        return None
    return bundled if bundled and Path(bundled).is_file() else None


@pytest.fixture(scope="session")
def ffmpeg_binary() -> str:
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("环境内没有可用的 FFmpeg")
    return ffmpeg


@pytest.fixture
def source_video(ffmpeg_binary: str, tmp_path: Path) -> Path:
    """用 lavfi 合成 8 秒测试片（彩条 + 连续正弦音），不依赖仓库里的任何媒体文件。"""
    path = tmp_path / "src.mp4"
    cmd = [
        ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=640x360:rate=25:duration=8",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=8",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True)
    return path
