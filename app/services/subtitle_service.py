"""Subtitle Service — 编排字幕生成全流程。

流程：ASR 语句 → 映射到输出时间轴（按保留段裁剪）→ 合并重叠 → 按行宽预切分
     → 样式白名单/安全区域 → 输出 ASS 或 SRT

映射规则修正：原实现只保留"起点落在保留段内"的语句，跨过剪切点的整句会被丢弃；
现在按与保留段落的**重叠**裁切，语义上是"输出里听得见的部分就要有字幕"。
"""

from __future__ import annotations

import re

from app.models.asr_result import ASRResult, Utterance
from app.models.edl import OutputInterval
from app.models.enums import SubtitleFormat
from app.models.subtitle_config import (
    SubtitleConfig,
    SubtitleLine,
    SubtitleStyle,
)
from app.services.subtitle.ass_generator import ASSGenerator
from app.services.subtitle.srt_generator import SRTGenerator
from app.utils.logging import get_logger

logger = get_logger("services.subtitle_service")

_DEFAULT_FONT = "Microsoft YaHei"
_DEFAULT_SAFE_RATIO = 0.05
#: 与 SubtitleStyle 白名单一致；调用方可注入真实系统字体集合
KNOWN_FONTS = frozenset({
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "Noto Sans SC",
    "PingFang SC",
    "Source Han Sans SC",
    "Arial",
    "sans-serif",
})
_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
#: CJK 断句优先在这些字符后换行
_CJK_BREAK_AFTER = "，。！？；：、,.!?;: "
_MIN_LINE_US = 400_000  # 0.4s：短于此的字幕人眼来不及读


class SubtitleServiceError(RuntimeError):
    pass


class SubtitleService:
    """字幕生成服务。"""

    @staticmethod
    def generate_subtitle_text(
        asr_result: ASRResult,
        output_intervals: list[OutputInterval],
    ) -> list[SubtitleLine]:
        """基于 ASR 结果和 EDL 输出区间生成字幕行。"""
        lines: list[SubtitleLine] = []

        for utt in asr_result.utterances:
            lines.extend(SubtitleService._map_utterance(utt, output_intervals))

        logger.info(
            "subtitle_text_generated",
            total_utterances=len(asr_result.utterances),
            output_lines=len(lines),
        )
        return lines

    @staticmethod
    def _kept_runs(
        output_intervals: list[OutputInterval],
    ) -> list[tuple[int, int, int, int]]:
        """把源与输出都连续的保留段并成一个 run，避免未删内容的边界把一行字幕劈成两行。

        返回 (source_start, source_end, output_start, output_end)。
        """
        ordered = sorted(output_intervals, key=lambda iv: iv.start_us)
        runs: list[list[int]] = []
        for iv in ordered:
            src_end = iv.source_start_us + iv.duration_us
            if runs:
                prev = runs[-1]
                if prev[1] == iv.source_start_us and prev[3] == iv.start_us:
                    prev[1] = src_end
                    prev[3] = iv.end_us
                    continue
            runs.append([iv.source_start_us, src_end, iv.start_us, iv.end_us])
        return [(r[0], r[1], r[2], r[3]) for r in runs]

    @staticmethod
    def _map_utterance(
        utt: Utterance,
        output_intervals: list[OutputInterval],
    ) -> list[SubtitleLine]:
        runs = SubtitleService._kept_runs(output_intervals)
        lines: list[SubtitleLine] = []

        for src_start, src_end, out_start, _out_end in runs:
            overlap_start = max(utt.start_us, src_start)
            overlap_end = min(utt.end_us, src_end)
            if overlap_end <= overlap_start:
                continue

            mapped_start = out_start + (overlap_start - src_start)
            mapped_end = out_start + (overlap_end - src_start)
            if mapped_end <= mapped_start:
                mapped_end = mapped_start + 1_000

            # 跨剪切点的语句只出现过半段时仍应显示文本；被完全剪掉的区间自然无重叠
            lines.append(
                SubtitleLine(
                    start_us=mapped_start,
                    end_us=mapped_end,
                    text=utt.text,
                )
            )

        return lines

    @staticmethod
    def resolve_overlaps(lines: list[SubtitleLine]) -> list[SubtitleLine]:
        """合并时间上真正重叠的字幕行。

        仅首尾相接（last.end == next.start）的行不合并：它们是两段不同的语音，
        合成一行既会超过行宽限制，也让 UI 无法逐条确认。
        """
        if not lines:
            return []

        sorted_lines = sorted(lines, key=lambda line: (line.start_us, line.end_us))
        merged: list[SubtitleLine] = [sorted_lines[0]]

        for line in sorted_lines[1:]:
            last = merged[-1]
            if line.start_us < last.end_us:
                merged[-1] = SubtitleLine(
                    start_us=last.start_us,
                    end_us=max(last.end_us, line.end_us),
                    text=SubtitleService._join_text(last.text, line.text),
                )
            else:
                merged.append(line)

        if len(merged) < len(lines):
            logger.info(
                "overlaps_resolved",
                original_count=len(lines),
                merged_count=len(merged),
            )
        return merged

    @staticmethod
    def _join_text(a: str, b: str) -> str:
        """中文之间不加空格，中英混排才用空格分隔。"""
        if not a:
            return b
        if not b:
            return a
        if _CJK.match(a[-1]) and _CJK.match(b[0]):
            return f"{a}{b}"
        return f"{a} {b}"

    @staticmethod
    def wrap_lines(lines: list[SubtitleLine], max_chars_per_line: int) -> list[SubtitleLine]:
        """按配置的每行最大字数预切分，时间按字符数比例分配。

        此前 max_chars_per_line 从未被读取，长句直接溢出画面。
        """
        if max_chars_per_line <= 0:
            return lines

        wrapped: list[SubtitleLine] = []
        for line in lines:
            chunks = SubtitleService._split_text(line.text, max_chars_per_line)
            if len(chunks) <= 1:
                wrapped.append(line)
                continue

            total_chars = sum(len(c) for c in chunks) or 1
            duration = line.end_us - line.start_us
            cursor = line.start_us
            for index, chunk in enumerate(chunks):
                slice_us = max(
                    1_000,
                    int(duration * len(chunk) / total_chars),
                )
                end_us = line.end_us if index == len(chunks) - 1 else cursor + slice_us
                if end_us <= cursor:
                    end_us = cursor + 1_000
                wrapped.append(
                    SubtitleLine(start_us=cursor, end_us=min(end_us, line.end_us), text=chunk)
                )
                cursor = min(end_us, line.end_us)

        return wrapped

    @staticmethod
    def _split_text(text: str, max_chars: int) -> list[str]:
        if len(text) <= max_chars:
            return [text]

        chunks: list[str] = []
        remaining = text
        while len(remaining) > max_chars:
            window = remaining[:max_chars]
            cut = SubtitleService._best_cut(window, max_chars)
            chunks.append(remaining[:cut].strip())
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return [c for c in chunks if c]

    @staticmethod
    def _best_cut(window: str, max_chars: int) -> int:
        """优先在标点后断行；英文单词不被劈开。"""
        for offset in range(len(window), max(1, len(window) - 6), -1):
            if window[offset - 1] in _CJK_BREAK_AFTER:
                return offset
        space = window.rfind(" ")
        if space > 0:
            return space + 1
        return max_chars

    @staticmethod
    def enforce_minimum_duration(
        lines: list[SubtitleLine],
        total_duration_us: int,
        minimum_us: int = _MIN_LINE_US,
    ) -> list[SubtitleLine]:
        """把过短的字幕延长到下一行开始之前，避免一闪而过。"""
        if not lines:
            return lines

        ordered = sorted(lines, key=lambda line: line.start_us)
        result: list[SubtitleLine] = []
        for index, line in enumerate(ordered):
            end_us = line.end_us
            if end_us - line.start_us < minimum_us:
                nxt = ordered[index + 1].start_us if index + 1 < len(ordered) else total_duration_us
                end_us = min(max(nxt, line.start_us + minimum_us), total_duration_us)
                if end_us <= line.start_us:
                    end_us = line.start_us + minimum_us
            result.append(
                SubtitleLine(start_us=line.start_us, end_us=end_us, text=line.text)
            )
        return result

    @staticmethod
    def clamp_to_safe_area(
        style: SubtitleStyle,
        width: int,
        height: int,
        h_ratio: float | None = None,
        v_ratio: float | None = None,
    ) -> SubtitleStyle:
        """竖屏安全区域 clamp：确保字幕边距不小于安全区域。

        始终返回副本，避免调用方拿到的对象与入参共享（早期实现在横向视频时
        直接返回同一个对象）。h_ratio/v_ratio 通常来自 SubtitleConfig 的安全边距，
        未提供时退到 5%。
        """
        new_style = style.model_copy(deep=True)
        if height <= width:
            return new_style

        horizontal = _DEFAULT_SAFE_RATIO if h_ratio is None else h_ratio
        vertical = _DEFAULT_SAFE_RATIO if v_ratio is None else v_ratio
        h_margin = int(width * max(0.0, min(float(horizontal), 0.5)))
        v_margin = int(height * max(0.0, min(float(vertical), 0.5)))

        changed = False
        if new_style.margin_l < h_margin:
            new_style.margin_l = h_margin
            changed = True
        if new_style.margin_r < h_margin:
            new_style.margin_r = h_margin
            changed = True
        if new_style.margin_v < v_margin:
            new_style.margin_v = v_margin
            changed = True

        if changed:
            logger.info(
                "safe_area_clamped",
                width=width,
                height=height,
                h_margin=h_margin,
                v_margin=v_margin,
            )
        return new_style

    @staticmethod
    def resolve_font(
        style: SubtitleStyle,
        available_fonts: frozenset[str] | set[str] | None = None,
    ) -> tuple[SubtitleStyle, bool]:
        """检查字体是否可用，不可用时降级为默认中文字体。

        返回 (resolved_style, fell_back)。available_fonts 可注入真实系统字体集合。
        """
        fonts = KNOWN_FONTS if available_fonts is None else available_fonts
        if style.font_name in fonts:
            return style.model_copy(deep=True), False

        logger.warning(
            "font_not_available",
            requested=style.font_name,
            fallback=_DEFAULT_FONT,
        )
        new_style = style.model_copy(deep=True)
        new_style.font_name = _DEFAULT_FONT
        return new_style, True

    @staticmethod
    def generate_subtitle(
        asr_result: ASRResult,
        output_intervals: list[OutputInterval],
        config: SubtitleConfig,
        width: int = 1920,
        height: int = 1080,
    ) -> tuple[str, bool]:
        """生成字幕文件。

        返回 (subtitle_content, used_srt_fallback)。
        """
        lines = SubtitleService.generate_subtitle_text(asr_result, output_intervals)
        lines = SubtitleService.resolve_overlaps(lines)
        total_out_us = max((iv.end_us for iv in output_intervals), default=0)
        lines = SubtitleService.enforce_minimum_duration(lines, total_out_us)
        lines = SubtitleService.wrap_lines(lines, config.max_chars_per_line)

        if not lines:
            logger.warning("no_subtitle_lines_generated")
            return "", False

        style, font_fell_back = SubtitleService.resolve_font(config.style)
        style = SubtitleService.clamp_to_safe_area(
            style,
            width,
            height,
            h_ratio=config.safe_margin_horizontal,
            v_ratio=config.safe_margin_vertical,
        )

        if config.format == SubtitleFormat.SRT:
            return SRTGenerator.generate(lines), False

        try:
            content = ASSGenerator.generate(
                lines,
                style,
                play_res_x=width,
                play_res_y=height,
            )
            return content, False
        except Exception as exc:
            # 降级要可见：调用方据第二个返回值提示用户样式被丢弃
            logger.error(
                "ass_generation_failed_fallback_to_srt",
                error=str(exc),
                line_count=len(lines),
            )
            try:
                content = SRTGenerator.generate(lines)
            except Exception as srt_exc:
                raise SubtitleServiceError(f"字幕生成失败: {srt_exc}") from srt_exc
            return content, True
