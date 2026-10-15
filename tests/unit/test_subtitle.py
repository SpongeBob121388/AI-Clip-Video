"""字幕生成与 ASS 渲染单元测试。"""

from __future__ import annotations

import pytest

from app.models.asr_result import ASRResult, Utterance
from app.models.edl import OutputInterval
from app.models.enums import ASREngine, ModelSize, SubtitleFormat
from app.models.subtitle_config import SubtitleConfig, SubtitleLine, SubtitleStyle
from app.services.subtitle.ass_generator import ASSGenerator
from app.services.subtitle.srt_generator import SRTGenerator
from app.services.subtitle_service import SubtitleService


def _make_utterance(
    uid: str = "u1",
    start_us: int = 0,
    end_us: int = 1_000_000,
    text: str = "测试文本",
) -> Utterance:
    return Utterance(utterance_id=uid, start_us=start_us, end_us=end_us, text=text)


def _make_asr_result(utterances: list[Utterance]) -> ASRResult:
    total = max((u.end_us for u in utterances), default=0)
    return ASRResult(
        engine=ASREngine.FASTER_WHISPER,
        language="zh",
        model_size=ModelSize.MEDIUM,
        utterances=utterances,
        total_duration_us=total,
    )


def _make_output_interval(
    start_us: int = 0,
    end_us: int = 10_000_000,
    source_start_us: int = 0,
) -> OutputInterval:
    return OutputInterval(
        start_us=start_us, end_us=end_us, source_start_us=source_start_us
    )


@pytest.mark.unit
class TestSubtitleLine:
    def test_creation(self) -> None:
        line = SubtitleLine(start_us=0, end_us=1_000_000, text="你好")
        assert line.text == "你好"
        assert line.duration_us == 1_000_000

    def test_invalid_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            SubtitleLine(start_us=100, end_us=100, text="x")

    @pytest.mark.parametrize(
        "start,end",
        [(0, 1), (100, 200), (0, 1_000_000)],
    )
    def test_valid_ranges(self, start: int, end: int) -> None:
        line = SubtitleLine(start_us=start, end_us=end, text="ok")
        assert line.start_us == start


@pytest.mark.unit
class TestSubtitleTextGeneration:
    def test_basic_mapping(self) -> None:
        asr = _make_asr_result([_make_utterance("u1", 0, 1_000_000, "你好世界")])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert len(lines) == 1
        assert lines[0].text == "你好世界"
        assert lines[0].start_us == 0
        assert lines[0].end_us == 1_000_000

    def test_offset_mapping(self) -> None:
        asr = _make_asr_result([_make_utterance("u1", 5_000_000, 6_000_000, "偏移")])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert lines[0].start_us == 5_000_000
        assert lines[0].end_us == 6_000_000

    def test_deleted_interval_excluded(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "保留"),
            _make_utterance("u2", 1_500_000, 2_500_000, "删除区间内"),
            _make_utterance("u3", 3_000_000, 4_000_000, "保留2"),
        ])
        intervals = [
            _make_output_interval(0, 1_000_000, 0),
            _make_output_interval(1_000_000, 2_000_000, 3_000_000),
        ]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert len(lines) == 2
        assert lines[0].text == "保留"
        assert lines[1].text == "保留2"

    def test_remapped_after_deletion(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "第一段"),
            _make_utterance("u2", 3_000_000, 4_000_000, "第二段"),
        ])
        intervals = [
            _make_output_interval(0, 1_000_000, 0),
            _make_output_interval(1_000_000, 2_000_000, 3_000_000),
        ]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert len(lines) == 2
        assert lines[0].start_us == 0
        assert lines[1].start_us == 1_000_000
        assert lines[1].text == "第二段"

    def test_empty_asr(self) -> None:
        asr = _make_asr_result([])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert lines == []

    def test_utterance_spanning_interval_boundary(self) -> None:
        asr = _make_asr_result([_make_utterance("u1", 900_000, 1_100_000, "跨边界")])
        intervals = [
            _make_output_interval(0, 1_000_000, 0),
            _make_output_interval(1_000_000, 2_000_000, 1_000_000),
        ]
        lines = SubtitleService.generate_subtitle_text(asr, intervals)
        assert len(lines) == 1
        assert lines[0].start_us == 900_000


@pytest.mark.unit
class TestASSGenerator:
    def test_basic_generation(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=2_000_000, text="你好世界"),
        ]
        style = SubtitleStyle()
        result = ASSGenerator.generate(lines, style)
        assert "[Script Info]" in result
        assert "[V4+ Styles]" in result
        assert "[Events]" in result
        assert "Dialogue:" in result
        assert "你好世界" in result

    def test_whitelist_fields_only(self) -> None:
        lines = [SubtitleLine(start_us=0, end_us=1_000_000, text="test")]
        style = SubtitleStyle(
            font_name="SimHei",
            font_size=36,
            primary_color="&H00FF0000",
            outline_color="&H000000FF",
            bold=True,
            italic=False,
            outline=3.0,
            shadow=0.5,
            alignment=8,
            margin_l=30,
            margin_r=30,
            margin_v=50,
        )
        result = ASSGenerator.generate(lines, style)
        assert "SimHei" in result
        assert "36" in result
        assert "&H00FF0000" in result
        assert "1,0" in result
        assert "3.0" in result
        assert "0.5" in result

    def test_time_format(self) -> None:
        lines = [SubtitleLine(start_us=0, end_us=65_500_000, text="long")]
        style = SubtitleStyle()
        result = ASSGenerator.generate(lines, style)
        assert "0:00:00.00" in result
        assert "0:01:05.50" in result

    def test_multiple_lines(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=1_000_000, text="第一句"),
            SubtitleLine(start_us=1_500_000, end_us=2_500_000, text="第二句"),
        ]
        style = SubtitleStyle()
        result = ASSGenerator.generate(lines, style)
        assert result.count("Dialogue:") == 2
        assert "第一句" in result
        assert "第二句" in result

    def test_empty_lines(self) -> None:
        result = ASSGenerator.generate([], SubtitleStyle())
        assert "[Events]" in result
        assert "Dialogue:" not in result

    def test_special_chars_escaped(self) -> None:
        lines = [SubtitleLine(start_us=0, end_us=1_000_000, text="test{br}")]
        style = SubtitleStyle()
        result = ASSGenerator.generate(lines, style)
        assert "\\{br\\}" in result

    def test_chinese_punctuation(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=1_000_000, text="你好，世界！"),
        ]
        style = SubtitleStyle()
        result = ASSGenerator.generate(lines, style)
        assert "你好，世界！" in result


@pytest.mark.unit
class TestSRTGenerator:
    def test_basic_generation(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=2_000_000, text="你好世界"),
        ]
        result = SRTGenerator.generate(lines)
        assert "1" in result
        assert "00:00:00,000 --> 00:00:02,000" in result
        assert "你好世界" in result

    def test_multiple_lines(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=1_000_000, text="第一句"),
            SubtitleLine(start_us=1_500_000, end_us=2_500_000, text="第二句"),
        ]
        result = SRTGenerator.generate(lines)
        assert "1\n" in result
        assert "2\n" in result
        assert "第一句" in result
        assert "第二句" in result

    def test_empty_lines(self) -> None:
        result = SRTGenerator.generate([])
        assert result == ""

    def test_time_format(self) -> None:
        lines = [SubtitleLine(start_us=0, end_us=3_661_500_000, text="long")]
        result = SRTGenerator.generate(lines)
        assert "00:00:00,000 --> 01:01:01,500" in result


@pytest.mark.unit
class TestOverlapResolution:
    def test_no_overlap(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=1_000_000, text="A"),
            SubtitleLine(start_us=1_500_000, end_us=2_500_000, text="B"),
        ]
        result = SubtitleService.resolve_overlaps(lines)
        assert len(result) == 2

    def test_overlap_merged(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=2_000_000, text="A"),
            SubtitleLine(start_us=1_000_000, end_us=3_000_000, text="B"),
        ]
        result = SubtitleService.resolve_overlaps(lines)
        assert len(result) == 1
        assert "A" in result[0].text
        assert "B" in result[0].text
        assert result[0].start_us == 0
        assert result[0].end_us == 3_000_000

    def test_adjacent_not_merged(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=1_000_000, text="A"),
            SubtitleLine(start_us=1_000_000, end_us=2_000_000, text="B"),
        ]
        result = SubtitleService.resolve_overlaps(lines)
        assert len(result) == 2

    def test_empty(self) -> None:
        assert SubtitleService.resolve_overlaps([]) == []

    def test_three_way_overlap(self) -> None:
        lines = [
            SubtitleLine(start_us=0, end_us=2_000_000, text="A"),
            SubtitleLine(start_us=1_000_000, end_us=3_000_000, text="B"),
            SubtitleLine(start_us=2_500_000, end_us=4_000_000, text="C"),
        ]
        result = SubtitleService.resolve_overlaps(lines)
        assert len(result) == 1
        assert result[0].end_us == 4_000_000


@pytest.mark.unit
class TestSafeAreaClamp:
    def test_horizontal_video_no_clamp(self) -> None:
        style = SubtitleStyle(margin_l=10, margin_r=10, margin_v=20)
        result = SubtitleService.clamp_to_safe_area(style, 1920, 1080)
        assert result.margin_l == 10
        assert result.margin_r == 10
        assert result.margin_v == 20

    def test_vertical_video_clamps_margins(self) -> None:
        style = SubtitleStyle(margin_l=10, margin_r=10, margin_v=20)
        result = SubtitleService.clamp_to_safe_area(style, 1080, 1920)
        h_safe = int(1080 * 0.05)
        v_safe = int(1920 * 0.05)
        assert result.margin_l == h_safe
        assert result.margin_r == h_safe
        assert result.margin_v == v_safe

    def test_margins_above_safe_unchanged(self) -> None:
        h_safe = int(1080 * 0.05)
        v_safe = int(1920 * 0.05)
        style = SubtitleStyle(
            margin_l=h_safe + 100,
            margin_r=h_safe + 100,
            margin_v=v_safe + 100,
        )
        result = SubtitleService.clamp_to_safe_area(style, 1080, 1920)
        assert result.margin_l == h_safe + 100
        assert result.margin_r == h_safe + 100
        assert result.margin_v == v_safe + 100

    def test_square_video_no_clamp(self) -> None:
        style = SubtitleStyle(margin_l=5, margin_r=5, margin_v=5)
        result = SubtitleService.clamp_to_safe_area(style, 1080, 1080)
        assert result.margin_l == 5
        assert result.margin_v == 5


@pytest.mark.unit
class TestFontResolution:
    def test_available_font(self) -> None:
        style = SubtitleStyle(font_name="SimHei")
        resolved, fell_back = SubtitleService.resolve_font(style)
        assert fell_back is False
        assert resolved.font_name == "SimHei"

    def test_unavailable_font_falls_back(self) -> None:
        style = SubtitleStyle(font_name="Microsoft YaHei")
        resolved, fell_back = SubtitleService.resolve_font(style)
        assert fell_back is False
        assert resolved.font_name == "Microsoft YaHei"


@pytest.mark.unit
class TestSubtitleServiceOrchestration:
    def test_generate_ass_subtitle(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "你好世界"),
        ])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        config = SubtitleConfig(format=SubtitleFormat.ASS)
        content, fallback = SubtitleService.generate_subtitle(
            asr, intervals, config, 1920, 1080
        )
        assert "[Script Info]" in content
        assert "你好世界" in content
        assert fallback is False

    def test_generate_srt_subtitle(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "你好世界"),
        ])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        config = SubtitleConfig(format=SubtitleFormat.SRT)
        content, fallback = SubtitleService.generate_subtitle(
            asr, intervals, config, 1920, 1080
        )
        assert "00:00:00,000" in content
        assert "你好世界" in content
        assert fallback is False

    def test_empty_result(self) -> None:
        asr = _make_asr_result([])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        config = SubtitleConfig()
        content, fallback = SubtitleService.generate_subtitle(
            asr, intervals, config
        )
        assert content == ""
        assert fallback is False

    def test_vertical_video_safe_area_applied(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "竖屏字幕"),
        ])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        config = SubtitleConfig(
            format=SubtitleFormat.ASS,
            style=SubtitleStyle(margin_l=10, margin_r=10, margin_v=20),
        )
        content, fallback = SubtitleService.generate_subtitle(
            asr, intervals, config, 1080, 1920
        )
        assert "[Script Info]" in content
        assert "竖屏字幕" in content
        # 安全边距取自 SubtitleConfig，而不是写死的 5%
        h_safe = int(1080 * config.safe_margin_horizontal)
        assert str(h_safe) in content
        assert "WrapStyle: 2" in content

    def test_overlaps_resolved_in_pipeline(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 2_000_000, "重叠A"),
            _make_utterance("u2", 1_000_000, 3_000_000, "重叠B"),
        ])
        intervals = [_make_output_interval(0, 10_000_000, 0)]
        config = SubtitleConfig(format=SubtitleFormat.SRT)
        content, _ = SubtitleService.generate_subtitle(
            asr, intervals, config
        )
        assert content.count("00:00:00,000") == 1
        assert "重叠A" in content
        assert "重叠B" in content

    def test_deleted_intervals_excluded_in_pipeline(self) -> None:
        asr = _make_asr_result([
            _make_utterance("u1", 0, 1_000_000, "保留"),
            _make_utterance("u2", 1_500_000, 2_500_000, "删除"),
            _make_utterance("u3", 3_000_000, 4_000_000, "保留2"),
        ])
        intervals = [
            _make_output_interval(0, 1_000_000, 0),
            _make_output_interval(1_000_000, 2_000_000, 3_000_000),
        ]
        config = SubtitleConfig(format=SubtitleFormat.SRT)
        content, _ = SubtitleService.generate_subtitle(
            asr, intervals, config
        )
        assert "保留" in content
        assert "保留2" in content
        assert "删除" not in content
