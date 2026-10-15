"""SRT 字幕生成器 — 生成符合 SRT 规范的字幕文件（无样式）。

作为 ASS 渲染失败的降级方案。SRT 用空行分条目，因此文本里的空行会直接把
一条字幕劈成两条并让后续编号错位；控制字符同理需要清掉。
"""

from __future__ import annotations

import re

from app.models.subtitle_config import SubtitleLine
from app.utils.logging import get_logger

logger = get_logger("services.subtitle.srt_generator")

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MIN_MS = 1


class SRTGenerator:
    """生成 SRT 格式字幕文件。"""

    @staticmethod
    def generate(lines: list[SubtitleLine]) -> str:
        entries: list[str] = []

        for i, line in enumerate(lines, start=1):
            start_ms = line.start_us // 1_000
            end_ms = max(line.end_us // 1_000, start_ms + _MIN_MS)
            entries.append(
                f"{i}\n{SRTGenerator._format_time_ms(start_ms)} --> "
                f"{SRTGenerator._format_time_ms(end_ms)}\n{SRTGenerator._escape_text(line.text)}"
            )

        logger.info("srt_generated", line_count=len(lines))
        return "\n\n".join(entries) + "\n" if entries else ""

    @staticmethod
    def _format_time_ms(total_ms: int) -> str:
        hours = total_ms // 3_600_000
        remaining = total_ms % 3_600_000
        minutes = remaining // 60_000
        remaining %= 60_000
        seconds = remaining // 1_000
        milliseconds = remaining % 1_000
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    @staticmethod
    def _format_time(us: int) -> str:
        return SRTGenerator._format_time_ms(us // 1_000)

    @staticmethod
    def _escape_text(text: str) -> str:
        cleaned = _CONTROL_CHARS.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
        # 折叠空行：条目内允许多行，但不允许出现空行分隔
        lines = [line.strip() for line in cleaned.split("\n")]
        return "\n".join(line for line in lines if line) or " "
