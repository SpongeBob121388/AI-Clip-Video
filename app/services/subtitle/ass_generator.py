"""ASS 字幕生成器 — 生成符合 ASS 规范的字幕文件。

仅使用白名单样式字段（spec 5.7.1.3），忽略任何非白名单字段。

转义规则参照 pysubs2：先转义反斜杠，再转义花括号，否则 ASR 文本里的
`\\N`、`{}` 会被 libass 当作换行/覆盖标签执行。
"""

from __future__ import annotations

import re

from app.models.subtitle_config import SubtitleLine, SubtitleStyle
from app.utils.logging import get_logger

logger = get_logger("services.subtitle.ass_generator")

# WrapStyle: 2 = 不做自动换行。字幕行已由 SubtitleService 按 max_chars_per_line
# 预切分并用 \N 排版，再让 libass 自动换行会造成二次断行。
_ASS_HEADER = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709
PlayResX: {play_res_x}
PlayResY: {play_res_y}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{font_size},{primary_colour},{secondary_colour},{outline_colour},{back_colour},{bold},{italic},0,0,100,100,0,0,1,{outline},{shadow},{alignment},{margin_l},{margin_r},{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# 控制字符（除 \n 外）会让整份 ASS 解析错位
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MIN_CENTISECONDS = 1


class ASSGenerator:
    """生成 ASS 格式字幕文件。"""

    @staticmethod
    def generate(
        lines: list[SubtitleLine],
        style: SubtitleStyle,
        play_res_x: int = 1920,
        play_res_y: int = 1080,
    ) -> str:
        ASSGenerator._validate_style(style)

        header = _ASS_HEADER.format(
            play_res_x=play_res_x,
            play_res_y=play_res_y,
            font_name=style.font_name,
            font_size=style.font_size,
            primary_colour=style.primary_color,
            secondary_colour="&H000000FF",
            outline_colour=style.outline_color,
            back_colour="&H00000000",
            bold=int(style.bold),
            italic=int(style.italic),
            outline=style.outline,
            shadow=style.shadow,
            alignment=style.alignment,
            margin_l=style.margin_l,
            margin_r=style.margin_r,
            margin_v=style.margin_v,
        )

        events: list[str] = []
        for line in lines:
            start_cs = ASSGenerator._to_centiseconds(line.start_us)
            end_cs = ASSGenerator._to_centiseconds(line.end_us)
            if end_cs <= start_cs:
                # 厘秒截断会把极短行变成 start==end 的不可见字幕
                end_cs = start_cs + _MIN_CENTISECONDS

            events.append(
                "Dialogue: 0,"
                f"{ASSGenerator._format_centiseconds(start_cs)},"
                f"{ASSGenerator._format_centiseconds(end_cs)},"
                f"Default,,0,0,0,,{ASSGenerator._escape_text(line.text)}"
            )

        logger.info(
            "ass_generated",
            line_count=len(lines),
            play_res=f"{play_res_x}x{play_res_y}",
        )
        return header + "\n".join(events) + "\n"

    @staticmethod
    def _validate_style(style: SubtitleStyle) -> None:
        """Style 行的字段以逗号分列，任何逗号都会让后续列整体错位。"""
        for field in ("font_name", "primary_color", "outline_color"):
            value = str(getattr(style, field))
            if "," in value or "\n" in value:
                raise ValueError(f"样式字段 {field} 含非法字符: {value!r}")

    @staticmethod
    def _to_centiseconds(us: int) -> int:
        return max(0, int(round(us / 10_000)))

    @staticmethod
    def _format_centiseconds(total_cs: int) -> str:
        hours = total_cs // 360_000
        remaining = total_cs % 360_000
        minutes = remaining // 6_000
        remaining %= 6_000
        seconds = remaining // 100
        centiseconds = remaining % 100
        return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    @staticmethod
    def _format_time(us: int) -> str:
        return ASSGenerator._format_centiseconds(ASSGenerator._to_centiseconds(us))

    @staticmethod
    def _escape_text(text: str) -> str:
        cleaned = _CONTROL_CHARS.sub("", text)
        return (
            cleaned.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\r\n", "\n")
            .replace("\n", "\\N")
        )
