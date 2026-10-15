"""FFmpeg/ffprobe Provider 单元测试。"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, patch

import pytest

from app.providers.ffmpeg.protocol import RendererProtocol
from app.providers.ffmpeg.runner import FFmpegError, FFmpegRunner, _normalize_rotation
from app.utils.path_validator import (
    PathValidationError,
    check_codec_supported,
    validate_media_path,
)

# 子进程调用已被 mock，这里只需要一个"确实存在"的可执行路径，
# 让 runner 的 ffprobe 可用性探测在缺少 ffprobe 的开发机上也能走 ffprobe 分支
EXE = sys.executable


@pytest.mark.unit
class TestPathValidator:
    def test_valid_path(self, tmp_path) -> None:
        f = tmp_path / "test.mp4"
        f.write_bytes(b"fake")
        result = validate_media_path(str(f))
        assert result == f

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(PathValidationError, match="空"):
            validate_media_path("")

    def test_relative_path_rejected(self) -> None:
        with pytest.raises(PathValidationError, match="绝对路径"):
            validate_media_path("relative/path.mp4")

    def test_traversal_rejected(self) -> None:
        with pytest.raises(PathValidationError, match="遍历"):
            validate_media_path("C:/Users/../etc/passwd.mp4")

    def test_network_stream_rejected(self) -> None:
        with pytest.raises(PathValidationError, match="网络流"):
            validate_media_path("rtmp://example.com/stream")

    def test_unsupported_extension_rejected(self, tmp_path) -> None:
        f = tmp_path / "test.flv"
        f.write_bytes(b"fake")
        with pytest.raises(PathValidationError, match="不支持"):
            validate_media_path(str(f))

    def test_nonexistent_file_rejected(self) -> None:
        with pytest.raises(PathValidationError, match="不存在"):
            validate_media_path("C:/nonexistent/file.mp4")


@pytest.mark.unit
class TestCodecCheck:
    def test_supported_codecs(self) -> None:
        assert check_codec_supported("h264", "aac") == []

    def test_unsupported_video_codec(self) -> None:
        warnings = check_codec_supported("prores", "aac")
        assert len(warnings) == 1
        assert "prores" in warnings[0]

    def test_unsupported_audio_codec(self) -> None:
        warnings = check_codec_supported("h264", "vorbis")
        assert len(warnings) == 1
        assert "vorbis" in warnings[0]

    def test_both_unsupported(self) -> None:
        warnings = check_codec_supported("prores", "vorbis")
        assert len(warnings) == 2


@pytest.mark.unit
class TestFFmpegRunner:
    def test_protocol_compliance(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        assert isinstance(runner, RendererProtocol)

    async def test_check_version(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "ffmpeg version 6.1.1\n", ""),
        ):
            major, minor = await runner.check_version()
            assert major == 6
            assert minor == 1

    async def test_check_version_failure(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(1, "", "error"),
        ), pytest.raises(FFmpegError):
            await runner.check_version()

    async def test_probe_success(self, tmp_path) -> None:
        f = tmp_path / "test.mp4"
        f.write_bytes(b"fake")

        mock_ffprobe_output = {
            "format": {"duration": "10.5", "bit_rate": "500000"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "30/1",
                    "time_base": "1/15360",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": 44100,
                    "time_base": "1/44100",
                },
            ],
        }

        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, json.dumps(mock_ffprobe_output), ""),
        ):
            probe = await runner.probe(str(f))

        assert probe.duration_us == 10_500_000
        assert probe.width == 1920
        assert probe.height == 1080
        assert probe.codec_video == "h264"
        assert probe.codec_audio == "aac"
        assert probe.is_vfr is False
        assert probe.rotation == 0
        assert len(probe.tracks) == 2
        assert probe.has_video is True
        assert probe.has_audio is True

    async def test_probe_vfr_detection(self, tmp_path) -> None:
        f = tmp_path / "vfr.mp4"
        f.write_bytes(b"fake")

        mock_output = {
            "format": {"duration": "5.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "29.97/1",
                    "time_base": "1/15360",
                }
            ],
        }

        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, json.dumps(mock_output), ""),
        ):
            probe = await runner.probe(str(f))

        assert probe.is_vfr is True
        assert probe.is_vertical is True

    async def test_probe_rotation(self, tmp_path) -> None:
        f = tmp_path / "rotated.mp4"
        f.write_bytes(b"fake")

        mock_output = {
            "format": {"duration": "3.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1080,
                    "height": 1920,
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "30/1",
                    "side_data_list": [{"rotation": 90}],
                }
            ],
        }

        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, json.dumps(mock_output), ""),
        ):
            probe = await runner.probe(str(f))

        assert probe.rotation == 90

    async def test_probe_multi_audio_tracks(self, tmp_path) -> None:
        f = tmp_path / "multi_audio.mp4"
        f.write_bytes(b"fake")

        mock_output = {
            "format": {"duration": "10.0"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "r_frame_rate": "30/1",
                    "avg_frame_rate": "30/1",
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": 48000,
                },
                {
                    "index": 2,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "sample_rate": 48000,
                },
            ],
        }

        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, json.dumps(mock_output), ""),
        ):
            probe = await runner.probe(str(f))

        audio_tracks = runner.get_audio_tracks(probe)
        assert len(audio_tracks) == 2

    async def test_probe_invalid_path(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with pytest.raises(PathValidationError):
            await runner.probe("not_absolute.mp4")

    async def test_extract_audio(self, tmp_path) -> None:
        f = tmp_path / "test.mp4"
        f.write_bytes(b"fake")
        out = tmp_path / "output.wav"

        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        with patch.object(
            runner,
            "_run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ):
            result = await runner.extract_audio(str(f), 0, str(out))

        assert result == str(out)


# 真实 `ffmpeg -i` 输出片段（imageio-ffmpeg 发行版不带 ffprobe，fallback 是主路径）
_FFMPEG_STDERR_PORTRAIT = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, 'IMG_0039.mp4':
  Metadata:
    major_brand     : qt
  Duration: 00:00:06.02, start: 0.000000, bitrate: 812 kb/s
  Stream #0:0[0x1](und): Audio: aac (LC) (mp4a / 0x6134706D), 44100 Hz, stereo, fltp, 161 kb/s (default)
  Stream #0:1[0x2](und): Video: h264 (Main) (avc1 / 0x31637661), yuv420p(tv, bt709, progressive), 568x320, 703 kb/s, 29.97 fps, 29.97 tbr, 600 tbn (default)
    Side data:
      displaymatrix: rotation of -90.00 degrees
"""

_FFMPEG_STDERR_UPRIGHT = _FFMPEG_STDERR_PORTRAIT.replace(
    "    Side data:\n      displaymatrix: rotation of -90.00 degrees\n", ""
)


@pytest.mark.unit
class TestFfmpegFallbackProbe:
    def test_rotation_parsed_from_displaymatrix(self) -> None:
        probe = FFmpegRunner()._parse_ffmpeg_stderr(_FFMPEG_STDERR_PORTRAIT, "C:/x/a.mp4")
        # -90 归一为 270，与 ffprobe 分支口径一致
        assert probe.rotation == 270
        assert (probe.width, probe.height) == (568, 320)
        assert probe.duration_us == 6_020_000

    def test_rotation_zero_without_displaymatrix(self) -> None:
        probe = FFmpegRunner()._parse_ffmpeg_stderr(_FFMPEG_STDERR_UPRIGHT, "C:/x/b.mp4")
        assert probe.rotation == 0
        assert (probe.width, probe.height) == (568, 320)

    def test_positive_and_180_degrees(self) -> None:
        for text, expected in (("+90.00", 90), ("180.00", 180)):
            stderr = _FFMPEG_STDERR_PORTRAIT.replace("-90.00", text)
            probe = FFmpegRunner()._parse_ffmpeg_stderr(stderr, "C:/x/c.mp4")
            assert probe.rotation == expected, text

    def test_normalize_rotation_snaps_float_noise(self) -> None:
        assert _normalize_rotation(90.00001) == 90
        assert _normalize_rotation(-90.0) == 270
        assert _normalize_rotation(0.0) == 0
        assert _normalize_rotation(360.0) == 0

    def test_vfr_detected_from_fps_and_tbr(self) -> None:
        """打包环境没有 ffprobe，fps/tbr 差异是唯一的 VFR 信号。"""
        vfr = _FFMPEG_STDERR_UPRIGHT.replace("29.97 fps, 29.97 tbr", "29.66 fps, 30 tbr")
        probe = FFmpegRunner()._parse_ffmpeg_stderr(vfr, "C:/x/vfr.mp4")
        assert probe.is_vfr is True
        assert probe.time_base == "1/600"

    def test_cfr_not_flagged(self) -> None:
        probe = FFmpegRunner()._parse_ffmpeg_stderr(_FFMPEG_STDERR_UPRIGHT, "C:/x/cfr.mp4")
        assert probe.is_vfr is False

    def test_rounding_noise_not_flagged_as_vfr(self) -> None:
        """29.97/30 这类打印舍入差不该被当成可变帧率。"""
        near = _FFMPEG_STDERR_UPRIGHT.replace("29.97 fps, 29.97 tbr", "29.97 fps, 30 tbr")
        assert FFmpegRunner()._parse_ffmpeg_stderr(near, "C:/x/n.mp4").is_vfr is False

    def test_missing_tbr_falls_back_to_fps_only(self) -> None:
        line = _FFMPEG_STDERR_UPRIGHT.replace(
            "29.97 fps, 29.97 tbr, 600 tbn", "29.97 fps"
        )
        probe = FFmpegRunner()._parse_ffmpeg_stderr(line, "C:/x/o.mp4")
        assert probe.fps == 29.97
        assert probe.is_vfr is False
        assert probe.time_base is None

    def test_probe_rotation_side_data_snaps_float(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        assert runner._parse_rotation({"side_data_list": [{"rotation": -90.0}]}) == 270
        assert runner._parse_rotation({"side_data_list": [{"rotation": "90.000001"}]}) == 90
        assert runner._parse_rotation({"side_data_list": [{"rotation": "n/a"}]}) == 0


class TestSelectorLoopFallback:
    """Windows 的 Selector 事件循环（uvicorn --reload）不能 spawn 子进程。

    这条路径必须真的执行命令，否则探测/渲染在那种进程里会整体失效。
    """

    async def test_run_subprocess_falls_back_to_threads(self) -> None:
        runner = FFmpegRunner(ffmpeg_path=EXE, ffprobe_path=EXE)
        argv = [EXE, "-c", "print('ok-from-thread')"]

        async def explode(*_args, **_kwargs):
            raise NotImplementedError

        with patch("asyncio.create_subprocess_exec", side_effect=explode):
            returncode, stdout, stderr = await runner._run_subprocess(argv, timeout_seconds=30)

        assert returncode == 0, stderr
        assert "ok-from-thread" in stdout

    def test_blocking_helper_streams_progress(self) -> None:
        from app.providers.ffmpeg.runner import _spawn_blocking

        script = (
            "import sys;"
            "print('out_time_us=1000000', flush=True);"
            "print('out_time_ms=3000', flush=True);"
            "print('progress=end')"
        )
        seen: list[int] = []
        rc, _stdout, stderr = _spawn_blocking(
            [EXE, "-c", script], 30, seen.append, lambda: False
        )
        assert rc == 0, stderr
        # out_time_ms 是 FFmpeg 的历史误名，值同样是微秒：乘 1000 会把进度放大一千倍
        assert seen == [1_000_000, 3_000]

    def test_blocking_helper_honours_should_stop(self) -> None:
        from app.providers.ffmpeg.runner import _spawn_blocking

        script = (
            "import time;"
            "print('out_time_us=1000', flush=True);"
            "time.sleep(5)"
        )
        with pytest.raises(FFmpegError, match="取消"):
            _spawn_blocking([EXE, "-c", script], 30, lambda _v: None, lambda: True)

    def test_blocking_helper_times_out(self) -> None:
        from app.providers.ffmpeg.runner import _spawn_blocking

        script = "import time; time.sleep(5)"
        with pytest.raises(FFmpegError, match="未完成"):
            _spawn_blocking([EXE, "-c", script], 0.5)
