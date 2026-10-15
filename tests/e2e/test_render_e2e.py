"""真实端到端渲染测试。

这条用例的存在理由：单元测试几乎全部把 `_run_subprocess` mock 掉了，于是
"354 个用例全绿"与"应用一按导出就崩"可以同时成立。这里不调用任何 mock，
用本机可解析到的 FFmpeg 真跑一遍 EDL → RenderPlan → 渲染 → 校验。

没有 FFmpeg 的环境会 skip，不会伪装成通过。
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from app.models.asr_result import ASRResult, Utterance
from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import (
    ASREngine,
    Decision,
    DecisionSource,
    ModelSize,
    SubtitleFormat,
    SuggestionStatus,
    SuggestionType,
)
from app.models.render_plan import RenderPreset
from app.models.subtitle_config import SubtitleConfig
from app.providers.ffmpeg.runner import FFmpegRunner
from app.services.edl_service import EDLService
from app.services.render_service import (
    RenderCancelledError,
    RenderExecutor,
    RenderService,
)
from app.services.subtitle_service import SubtitleService
from tests.e2e.conftest import SOURCE_DURATION_US

#: 删除 [2s,3s) 与 [5s,5.5s) → 6.5s
DELETE_RANGES = [(2_000_000, 3_000_000), (5_000_000, 5_500_000)]
EXPECTED_OUT_US = SOURCE_DURATION_US - 1_000_000 - 500_000

pytestmark = pytest.mark.e2e


def _suggestion(sid: str, start: int, end: int) -> Suggestion:
    return Suggestion(
        id=sid,
        source_start_us=start,
        source_end_us=end,
        suggestion_type=SuggestionType.SILENCE,
        decision=Decision.DELETE,
        status=SuggestionStatus.ACCEPTED,
        source=DecisionSource.MANUAL,
    )


def _make_edl() -> EDL:
    return EDL(
        source=EDLSource(uri="src.mp4", duration_us=SOURCE_DURATION_US),
        suggestions=[
            _suggestion(f"cut-{index}", start, end)
            for index, (start, end) in enumerate(DELETE_RANGES)
        ],
    )


def _runner(ffmpeg_binary: str) -> FFmpegRunner:
    # ffprobe 路径同样指向 ffmpeg：探测会走 stderr 解析分支，覆盖真实回退路径
    return FFmpegRunner(ffmpeg_path=ffmpeg_binary, ffprobe_path=ffmpeg_binary)


@pytest.mark.asyncio
async def test_render_export_produces_frame_accurate_video(
    source_video: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    runner = _runner(ffmpeg_binary)
    executor = RenderExecutor(runner=runner, work_root=tmp_path / "work")

    edl = _make_edl()
    intervals = EDLService.compute_output_intervals(edl)
    assert RenderService.expected_output_duration_us(intervals) == EXPECTED_OUT_US

    out = tmp_path / "out.mp4"
    plan = RenderService.generate_export_plan(
        edl,
        uuid4(),
        RenderPreset(),
        source_file_path=str(source_video),
        output_file_path=str(out),
        has_audio=True,
    )

    progress: list[tuple[float, str]] = []
    outcome = await executor.execute(
        plan,
        source_path=str(source_video),
        output_path=str(out),
        on_progress=lambda value, message: progress.append((value, message)),
    )

    assert out.is_file()
    assert outcome.size_bytes > 0
    # 帧精确：允许 2% / 0.5s 的容差，但绝不能是关键帧级别的整段偏移
    assert abs(outcome.duration_us - EXPECTED_OUT_US) < 400_000, outcome.duration_us
    # 进度由 -progress 的 out_time 驱动，必须真的走完
    assert progress and max(value for value, _ in progress) >= 0.9
    rendered_seconds = [
        float(message.split()[-1].rstrip("s"))
        for _, message in progress
        if message.startswith("已渲染")
    ]
    # out_time_ms 与 out_time_us 同为微秒；把它当毫秒会让文案里的秒数放大一千倍
    assert rendered_seconds, progress
    assert max(rendered_seconds) <= EXPECTED_OUT_US / 1_000_000 + 0.5, rendered_seconds


@pytest.mark.asyncio
async def test_render_with_burned_subtitles(
    source_video: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    """烧录字幕走 filtergraph 里的路径参数，是注入面也是易碎点。"""
    runner = _runner(ffmpeg_binary)
    executor = RenderExecutor(runner=runner, work_root=tmp_path / "work")

    edl = _make_edl()
    intervals = EDLService.compute_output_intervals(edl)
    asr = ASRResult(
        engine=ASREngine.FASTER_WHISPER,
        model_size=ModelSize.TINY,
        utterances=[
            Utterance(utterance_id="u1", start_us=0, end_us=1_900_000, text="第一段，大家好"),
            Utterance(utterance_id="u2", start_us=3_200_000, end_us=4_800_000, text="第二段\\N含转义"),
        ],
    )
    config = SubtitleConfig(format=SubtitleFormat.ASS)
    content, fell_back = SubtitleService.generate_subtitle(
        asr, intervals, config, width=640, height=360
    )
    assert content and fell_back is False

    plan_id = uuid4()
    subtitle_path = await executor.write_subtitle_file(plan_id, content)
    out = tmp_path / "subbed.mp4"
    plan = RenderService.generate_export_plan(
        edl,
        plan_id,
        RenderPreset(),
        source_file_path=str(source_video),
        subtitle_file_path=str(subtitle_path),
        output_file_path=str(out),
    )

    outcome = await executor.execute(
        plan,
        source_path=str(source_video),
        output_path=str(out),
        subtitle_path=str(subtitle_path),
    )
    assert out.is_file()
    assert abs(outcome.duration_us - EXPECTED_OUT_US) < 400_000


@pytest.mark.asyncio
async def test_cancellation_stops_render(
    source_video: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    runner = _runner(ffmpeg_binary)
    executor = RenderExecutor(runner=runner, work_root=tmp_path / "work")

    edl = _make_edl()
    out = tmp_path / "cancelled.mp4"
    plan = RenderService.generate_export_plan(
        edl,
        uuid4(),
        RenderPreset(),
        source_file_path=str(source_video),
        output_file_path=str(out),
    )

    with pytest.raises(RenderCancelledError):
        await executor.execute(
            plan,
            source_path=str(source_video),
            output_path=str(out),
            should_stop=lambda: True,
        )


@pytest.mark.asyncio
async def test_probe_falls_back_to_ffmpeg_when_no_ffprobe(
    source_video: Path, ffmpeg_binary: str
) -> None:
    """没有 ffprobe 时必须能用 ffmpeg -i 解析出时长，而不是回退成'探测成功但全 0'。"""
    runner = FFmpegRunner(
        ffmpeg_path=ffmpeg_binary,
        ffprobe_path=str(source_video.parent / "no-ffprobe"),
    )
    probe = await runner.probe(str(source_video))
    assert abs(probe.duration_us - SOURCE_DURATION_US) < 300_000
    assert probe.has_video and probe.has_audio
    assert probe.width == 640 and probe.height == 360
