"""帧率 / CRF / 音轨选择的真实渲染验证。

单元测试只能证明 argv 的形状；只有真渲染才能证明 `[0:a:1]` 和 `fps=50` 是 FFmpeg 认的写法，
并且产物里真的是那条音、真的是那个帧率。没有 FFmpeg 时整组 skip，不伪装通过。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.edl import EDL, EDLSource
from app.models.render_plan import RenderPreset
from app.providers.ffmpeg.runner import FFmpegRunner
from app.services.render_service import RenderExecutor, RenderService

pytestmark = pytest.mark.e2e

_DURATION_US = 4_000_000


def _runner(ffmpeg_binary: str) -> FFmpegRunner:
    # ffprobe 指向 ffmpeg：走的是随包产物同一条 stderr 解析路径
    return FFmpegRunner(ffmpeg_path=ffmpeg_binary, ffprobe_path=ffmpeg_binary)


@pytest.fixture
def two_audio_source(ffmpeg_binary: str, tmp_path: Path) -> Path:
    """两条音轨：第 0 条纯静音，第 1 条 440Hz 正弦。选错轨会被"输出是否静音"暴露。"""
    path = tmp_path / "two_audio.mp4"
    subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
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
        ],
        check=True,
    )
    return path


def _edl() -> EDL:
    return EDL(source=EDLSource(uri="two_audio.mp4", duration_us=_DURATION_US))


async def _render(
    source: Path,
    preset: RenderPreset,
    out: Path,
    ffmpeg_binary: str,
    tmp_path: Path,
):
    plan = RenderService.generate_export_plan(
        _edl(),
        uuid4(),
        preset,
        source_file_path=str(source),
        output_file_path=str(out),
        has_audio=True,
    )
    executor = RenderExecutor(runner=_runner(ffmpeg_binary), work_root=tmp_path / "work")
    outcome = await executor.execute(
        plan, source_path=str(source), output_path=str(out), on_progress=lambda v, m: None
    )
    return plan, outcome


def _silence_detected(ffmpeg_binary: str, path: Path) -> bool:
    """用 silencedetect 判断输出里那条音到底是不是静音。"""
    result = subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-45dB:d=0.5",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    return "silence_start" in result.stderr


@pytest.mark.asyncio
async def test_source_actually_has_two_audio_tracks(
    two_audio_source: Path, ffmpeg_binary: str
) -> None:
    """前置条件：合成的素材真有两条音轨，否则后面的用例是空跑。"""
    probe = await _runner(ffmpeg_binary).probe(str(two_audio_source))
    assert sum(1 for t in probe.tracks if t.codec_type == "audio") == 2


@pytest.mark.asyncio
async def test_selecting_the_second_track_outputs_audible_sound(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    _plan, outcome = await _render(
        two_audio_source, RenderPreset(audio_track_index=1), tmp_path / "t1.mp4", ffmpeg_binary, tmp_path
    )
    assert outcome.size_bytes > 0
    assert not _silence_detected(ffmpeg_binary, tmp_path / "t1.mp4")


@pytest.mark.asyncio
async def test_selecting_the_first_track_outputs_silence(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    await _render(
        two_audio_source, RenderPreset(audio_track_index=0), tmp_path / "t0.mp4", ffmpeg_binary, tmp_path
    )
    assert _silence_detected(ffmpeg_binary, tmp_path / "t0.mp4")


@pytest.mark.asyncio
async def test_default_preset_keeps_the_first_track(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    await _render(two_audio_source, RenderPreset(), tmp_path / "default.mp4", ffmpeg_binary, tmp_path)
    assert _silence_detected(ffmpeg_binary, tmp_path / "default.mp4")


@pytest.mark.asyncio
async def test_fps_option_resamples_the_output(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    await _render(
        two_audio_source, RenderPreset(fps=50), tmp_path / "fps50.mp4", ffmpeg_binary, tmp_path
    )
    out_probe = await _runner(ffmpeg_binary).probe(str(tmp_path / "fps50.mp4"))
    assert out_probe.fps is not None
    assert abs(out_probe.fps - 50.0) < 1.0, out_probe.fps


@pytest.mark.asyncio
async def test_without_fps_the_source_rate_is_kept(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    await _render(two_audio_source, RenderPreset(), tmp_path / "same.mp4", ffmpeg_binary, tmp_path)
    out_probe = await _runner(ffmpeg_binary).probe(str(tmp_path / "same.mp4"))
    assert out_probe.fps is not None
    assert abs(out_probe.fps - 25.0) < 1.0, out_probe.fps


@pytest.mark.asyncio
async def test_crf_actually_changes_the_bitrate(
    two_audio_source: Path, tmp_path: Path, ffmpeg_binary: str
) -> None:
    _plan_a, sharp = await _render(
        two_audio_source, RenderPreset(crf=16), tmp_path / "sharp.mp4", ffmpeg_binary, tmp_path
    )
    _plan_b, soft = await _render(
        two_audio_source, RenderPreset(crf=34), tmp_path / "soft.mp4", ffmpeg_binary, tmp_path
    )
    # 同一段噪点素材：CRF 差 18 档却几乎一样大，说明旋钮没落到 argv 上
    assert sharp.size_bytes > soft.size_bytes * 1.5, (sharp.size_bytes, soft.size_bytes)
