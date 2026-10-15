"""FFmpegRunner — FFmpeg/ffprobe 命令行安全调用封装。

使用 subprocess.run(argv, shell=False) 安全调用，路径作为独立 argv 元素。
当 ffprobe 不可用时，自动 fallback 到 ffmpeg -i stderr 解析。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.models.media import MediaProbe, MediaTrack
from app.utils.logging import get_logger
from app.utils.path_validator import (
    check_codec_supported,
    validate_media_path,
)

logger = get_logger("providers.ffmpeg.runner")

_FPS_PATTERN = re.compile(r"^(\d+)/(\d+)$")
_MIN_FFMPEG_VERSION = (6, 0)
_DURATION_PATTERN = re.compile(
    r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)"
)
_STREAM_PATTERN = re.compile(
    r"Stream\s+#\d+:\d+(?:\[[^\]]+\])?(?:\(\w+\))?:\s*(\w+):\s*(\w+)"
)
# FFmpeg 7.x 在分辨率后接 " [SAR 1:1 DAR 16:9]" 而非逗号，结尾不能要求逗号
_RESOLUTION_PATTERN = re.compile(r",\s*(\d{2,5})x(\d{2,5})\b")
_FPS_TOKEN_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*fps")
# ffmpeg -i 打印 "29.66 fps, 30 tbr, 15360 tbn"：fps=平均帧率、tbr=基准帧率、tbn=时间基分母。
# 没有 ffprobe 时（imageio-ffmpeg 只发 ffmpeg），这是判断 VFR 与取时间基的唯一来源
_FRAME_RATE_TRIPLE_PATTERN = re.compile(
    r"(\d+(?:\.\d+)?)\s*fps,\s*(\d+(?:\.\d+)?)\s*tbr,\s*(\d+)\s*tbn"
)
_VFR_RELATIVE_TOLERANCE = 0.005
_AUDIO_INFO_PATTERN = re.compile(r"(\d+)\s*Hz,\s*(\w+)")
_BITRATE_PATTERN = re.compile(r"bitrate:\s*(\d+)\s*kb/s")
# ffmpeg -i 打印的是 "displaymatrix: rotation of -90.00 degrees"（缩进在 Stream 行之下）
_ROTATION_PATTERN = re.compile(
    r"displaymatrix:\s*rotation of\s*([+-]?\d+(?:\.\d+)?)\s*degrees", re.IGNORECASE
)


def _normalize_rotation(degrees: float) -> int:
    """把 display matrix 角度吸附到 0/90/180/270。

    部分手机写入 90.00001 这类浮点值，而 EDLSource.rotation 只接受正交角度，
    原样传入会在后续校验里抛错并让整次导入失败。
    """
    return int(round(degrees / 90.0)) * 90 % 360


def _resolve_executable(configured: str, *, ffmpeg_fallback: bool) -> str:
    """解析可执行文件：配置值 → PATH → imageio_ffmpeg 自带的 ffmpeg。

    找不到时仍返回原值，但 `_run_subprocess` 会在执行前给出明确错误，
    而不是抛出一个没有诊断价值的 FileNotFoundError。
    """
    if configured and (shutil.which(configured) or Path(configured).is_file()):
        return configured
    if ffmpeg_fallback:
        try:
            import imageio_ffmpeg

            bundled = imageio_ffmpeg.get_ffmpeg_exe()
            if bundled and Path(bundled).is_file():
                return bundled
        except Exception as exc:  # noqa: BLE001 - 兜底路径本身不可靠
            logger.debug("imageio_ffmpeg_lookup_failed", error=str(exc))
    return configured


def _executable_exists(program: str) -> bool:
    if not program:
        return False
    return bool(shutil.which(program)) or Path(program).is_file()


def _decode_output(raw: bytes) -> str:
    """FFmpeg 在中文 Windows 上按系统代码页输出，先 UTF-8 再 GBK。"""
    for encoding in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _ffprobe_available(path: str) -> bool:
    """检查 ffprobe 是否可执行。"""
    if not path:
        return False
    return _executable_exists(path)


class FFmpegError(RuntimeError):
    pass


def _creation_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def _kill_blocking(process: subprocess.Popen) -> None:  # type: ignore[type-arg]
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=3)


def _spawn_blocking(
    argv: list[str],
    timeout_seconds: float | None,
    on_time_us: Any = None,
    should_stop: Any = None,
) -> tuple[int, str, str]:
    """线程 + 同步 subprocess 的兜底执行路径。

    Windows 上的 Selector 事件循环（uvicorn --reload / --workers 会强制切换循环策略）
    调用 `asyncio.create_subprocess_exec` 直接抛 NotImplementedError —— 若不兜底，
    整个 FFmpeg 能力在这类进程里等于没有：探测和渲染都会失败。
    """
    program = Path(argv[0]).name
    streaming = on_time_us is not None or should_stop is not None

    if not streaming:
        process = subprocess.Popen(  # noqa: S603 - argv 固定，绝不经过 shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_creation_flags(),
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_blocking(process)
            raise FFmpegError(f"{program} 超过 {timeout_seconds:.0f}s 未完成，已终止") from None
        return process.returncode, _decode_output(stdout), _decode_output(stderr)

    # 进度模式：stderr 落临时文件，避免只读 stdout 时管道写满把 FFmpeg 卡住
    err_fd, err_path = tempfile.mkstemp(prefix="acv_ffmpeg_err_", suffix=".log")
    cancelled = False
    with os.fdopen(err_fd, "wb") as err_file:
        process = subprocess.Popen(  # noqa: S603 - argv 固定，绝不经过 shell
            argv,
            stdout=subprocess.PIPE,
            stderr=err_file,
            creationflags=_creation_flags(),
        )
        assert process.stdout is not None
        deadline = time.monotonic() + (timeout_seconds or 0)
        try:
            for raw in process.stdout:
                line = _decode_output(raw).strip()
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    # FFmpeg 的 out_time_ms 是历史误名，实测与 out_time_us 同为微秒，不能再乘 1000
                    value = line.partition("=")[2]
                    if value.isdigit() and on_time_us is not None:
                        on_time_us(int(value))
                if should_stop is not None and should_stop():
                    cancelled = True
                    break
                if timeout_seconds and time.monotonic() > deadline:
                    raise FFmpegError(f"{program} 超过 {timeout_seconds:.0f}s 未完成，已终止")
        except BaseException:
            cancelled = cancelled or True
            _kill_blocking(process)
            raise
        finally:
            if cancelled:
                _kill_blocking(process)
            with contextlib.suppress(Exception):
                process.stdout.close()
            process.wait()
        with open(err_path, "rb") as fh:
            stderr_text = _decode_output(fh.read())
        with contextlib.suppress(OSError):
            os.unlink(err_path)

    if cancelled:
        raise FFmpegError("渲染已取消")
    return process.returncode, "", stderr_text


class FFmpegRunner:
    def __init__(
        self,
        ffmpeg_path: str | None = None,
        ffprobe_path: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        settings = get_settings()
        self.ffmpeg_path = ffmpeg_path or _resolve_executable(
            settings.ffmpeg_path, ffmpeg_fallback=True
        )
        self.ffprobe_path = ffprobe_path or _resolve_executable(
            settings.ffprobe_path, ffmpeg_fallback=False
        )
        self._ffprobe_ok = _ffprobe_available(self.ffprobe_path)
        self._timeout = float(timeout_seconds or settings.render_timeout_seconds)

    async def _run_subprocess(
        self, argv: list[str], timeout_seconds: float | None = None
    ) -> tuple[int, str, str]:
        if not _executable_exists(argv[0]):
            raise FFmpegError(
                f"未找到可执行文件 {argv[0]!r}，请在设置中指定 FFmpeg/ffprobe 路径或安装 ffmpeg-python 依赖"
            )
        try:
            return await self._run_subprocess_async(argv, timeout_seconds)
        except NotImplementedError:
            logger.warning("event_loop_cannot_spawn_subprocess", program=Path(argv[0]).name)
            return await asyncio.to_thread(
                _spawn_blocking, argv, timeout_seconds or self._timeout
            )

    async def _run_subprocess_async(
        self, argv: list[str], timeout_seconds: float | None = None
    ) -> tuple[int, str, str]:
        program = argv[0]

        creationflags = 0
        if sys.platform == "win32":
            # 桌面应用里避免闪出控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as exc:
            await self._terminate(process)
            raise FFmpegError(
                f"{Path(program).name} 超过 {timeout_seconds:.0f}s 未完成，已终止"
            ) from exc
        except asyncio.CancelledError:
            # 上层任务被取消时不能留下孤儿进程
            await self._terminate(process)
            raise
        except BaseException:
            await self._terminate(process)
            raise

        returncode = process.returncode if process.returncode is not None else -1
        return returncode, _decode_output(stdout), _decode_output(stderr)

    async def run_with_progress(
        self,
        argv: list[str],
        on_time_us: Any = None,
        should_stop: Any = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str]:
        """流式执行长任务，用 ffmpeg `-progress` 输出驱动进度与取消。"""
        if not _executable_exists(argv[0]):
            raise FFmpegError(f"未找到可执行文件 {argv[0]!r}")

        timeout = timeout_seconds or self._timeout
        try:
            return await self._run_with_progress_async(
                argv, on_time_us, should_stop, timeout
            )
        except NotImplementedError:
            logger.warning("event_loop_cannot_spawn_subprocess", program=Path(argv[0]).name)
            returncode, _stdout, stderr = await asyncio.to_thread(
                _spawn_blocking, argv, timeout, on_time_us, should_stop
            )
            return returncode, stderr

    async def _run_with_progress_async(
        self,
        argv: list[str],
        on_time_us: Any = None,
        should_stop: Any = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, str]:
        """stderr 落到临时文件而不是管道：一边只读 stdout 一边不排空 stderr 时，
        FFmpeg 会在管道写满后卡死。"""
        timeout_seconds = timeout_seconds or self._timeout
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        err_fd, err_path = tempfile.mkstemp(prefix="acv_ffmpeg_err_", suffix=".log")
        err_file = os.fdopen(err_fd, "wb")

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=err_file,
                creationflags=creationflags,
            )
        except NotImplementedError:
            # 兜底路径会自己管理 stderr 文件，这里不能留下临时文件
            err_file.close()
            with contextlib.suppress(OSError):
                os.unlink(err_path)
            raise
        cancelled = False
        try:
            async def _pump() -> None:
                nonlocal cancelled
                assert process.stdout is not None
                while True:
                    if should_stop is not None and should_stop():
                        cancelled = True
                        await self._terminate(process)
                        return
                    raw = await process.stdout.readline()
                    if not raw:
                        return
                    line = _decode_output(raw).strip()
                    if on_time_us is None:
                        continue
                    if line.startswith(("out_time_us=", "out_time_ms=")):
                        # 同上：out_time_ms 与 out_time_us 都是微秒
                        value = line.partition("=")[2]
                        if value.isdigit():
                            on_time_us(int(value))

            try:
                await asyncio.wait_for(_pump(), timeout=timeout_seconds)
            except TimeoutError:
                await self._terminate(process)
                raise FFmpegError(f"渲染超过 {timeout_seconds:.0f}s，已终止") from None
            await process.wait()
        finally:
            err_file.close()
            with open(err_path, "rb") as fh:
                stderr_text = _decode_output(fh.read())
            with contextlib.suppress(OSError):
                os.unlink(err_path)

        if cancelled:
            raise FFmpegError("渲染已取消")

        returncode = process.returncode if process.returncode is not None else -1
        return returncode, stderr_text

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """先温和终止，超时后强杀。"""
        if process.returncode is not None:
            return
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except Exception:  # noqa: BLE001 - 终止失败只能硬杀
            try:
                process.kill()
            except ProcessLookupError:
                return
            except Exception:  # pragma: no cover
                logger.error("ffmpeg_process_kill_failed", pid=process.pid)
                return
        logger.warning("ffmpeg_process_terminated", pid=process.pid)

    async def check_version(self) -> tuple[int, int]:
        returncode, stdout, _ = await self._run_subprocess(
            [self.ffmpeg_path, "-version"], timeout_seconds=30.0
        )
        if returncode != 0:
            raise FFmpegError("无法获取 FFmpeg 版本")
        first_line = stdout.split("\n")[0]
        match = re.search(r"ffmpeg version (\d+)\.(\d+)", first_line)
        if not match:
            raise FFmpegError(f"无法解析 FFmpeg 版本: {first_line}")
        return int(match.group(1)), int(match.group(2))

    async def ensure_compatible_version(self) -> None:
        major, minor = await self.check_version()
        if (major, minor) < _MIN_FFMPEG_VERSION:
            raise FFmpegError(
                f"FFmpeg 版本 {major}.{minor} 低于最低要求 {_MIN_FFMPEG_VERSION[0]}.{_MIN_FFMPEG_VERSION[1]}"
            )
        logger.info("ffmpeg_version_ok", major=major, minor=minor)

    async def probe(self, file_path: str) -> MediaProbe:
        path = validate_media_path(file_path)

        try:
            return await self._probe_with_ffprobe(str(path))
        except (FileNotFoundError, OSError) as exc:
            logger.warn("ffprobe_not_available_fallback_to_ffmpeg", error=str(exc))
        except Exception as exc:
            logger.warn("ffprobe_failed_fallback_to_ffmpeg", error=str(exc))

        return await self._probe_with_ffmpeg(str(path))

    async def _probe_with_ffprobe(self, file_path: str) -> MediaProbe:
        if not self._ffprobe_ok:
            raise FFmpegError(f"ffprobe 不可用: {self.ffprobe_path}")

        argv = [
            self.ffprobe_path,
            "-v", "quiet",
            "-show_format",
            "-show_streams",
            "-print_format", "json",
            file_path,
        ]
        returncode, stdout, stderr = await self._run_subprocess(
            argv, timeout_seconds=min(self._timeout, 120.0)
        )

        if returncode != 0:
            raise FFmpegError(f"ffprobe 失败: {stderr}")

        data = json.loads(stdout)
        return self._parse_probe_result(data, file_path)

    async def _probe_with_ffmpeg(self, file_path: str) -> MediaProbe:
        """使用 ffmpeg -i 解析 stderr 获取媒体信息（ffprobe 不可用时的 fallback）。

        注意 `ffmpeg -i x` 缺少输出参数必然以非 0 退出，因此以"是否解析出内容"判定成功。
        """
        argv = [
            self.ffmpeg_path,
            "-hide_banner",
            "-i", file_path,
        ]
        _, stdout, stderr = await self._run_subprocess(
            argv, timeout_seconds=min(self._timeout, 120.0)
        )

        probe = self._parse_ffmpeg_stderr(stderr, file_path)
        if probe.duration_us == 0 and not probe.tracks:
            raise FFmpegError(f"无法解析媒体信息（ffmpeg 与 ffprobe 均未取得有效结果）: {file_path}")
        return probe

    def _parse_ffmpeg_stderr(self, stderr: str, file_path: str) -> MediaProbe:
        """解析 ffmpeg -i 的 stderr 输出。"""
        lines = stderr.split("\n")

        duration_us = 0
        bit_rate = None
        width = None
        height = None
        codec_video = None
        codec_audio = None
        fps = None
        rotation = 0
        has_displaymatrix = False
        is_vfr = False
        time_base: str | None = None
        tracks: list[MediaTrack] = []

        for line in lines:
            line = line.strip()

            rot_match = _ROTATION_PATTERN.search(line)
            if rot_match:
                rotation = _normalize_rotation(float(rot_match.group(1)))
            elif "displaymatrix" in line:
                has_displaymatrix = True

            dur_match = _DURATION_PATTERN.search(line)
            if dur_match:
                h, m, s = dur_match.group(1), dur_match.group(2), dur_match.group(3)
                duration_us = int(
                    (int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000
                )
                br_match = _BITRATE_PATTERN.search(line)
                if br_match:
                    bit_rate = int(br_match.group(1)) * 1000

            stream_match = _STREAM_PATTERN.search(line)
            if stream_match:
                codec_type = stream_match.group(1).lower()
                codec_name = stream_match.group(2)
                track_index = len(tracks)

                if codec_type == "video":
                    codec_video = codec_name
                    res_match = _RESOLUTION_PATTERN.search(line)
                    if res_match:
                        width = int(res_match.group(1))
                        height = int(res_match.group(2))
                    triple_match = _FRAME_RATE_TRIPLE_PATTERN.search(line)
                    if triple_match:
                        fps = float(triple_match.group(1))
                        tbr = float(triple_match.group(2))
                        tbn = int(triple_match.group(3))
                        # 平均帧率偏离基准帧率 => 可变帧率，与 ffprobe 分支同一判据
                        is_vfr = tbr > 0 and abs(fps - tbr) / tbr > _VFR_RELATIVE_TOLERANCE
                        if tbn > 0:
                            time_base = f"1/{tbn}"
                    else:
                        fps_match = _FPS_TOKEN_PATTERN.search(line)
                        if fps_match:
                            fps = float(fps_match.group(1))
                    tracks.append(MediaTrack(
                        index=track_index,
                        codec_type="video",
                        codec_name=codec_name,
                        width=width,
                        height=height,
                        fps=fps,
                        time_base=time_base,
                    ))
                elif codec_type == "audio":
                    codec_audio = codec_name
                    ai_match = _AUDIO_INFO_PATTERN.search(line)
                    sample_rate = None
                    channels = None
                    if ai_match:
                        sample_rate = int(ai_match.group(1))
                        ch_str = ai_match.group(2)
                        channels = {"mono": 1, "stereo": 2}.get(ch_str)
                    tracks.append(MediaTrack(
                        index=track_index,
                        codec_type="audio",
                        codec_name=codec_name,
                        sample_rate=sample_rate,
                        channels=channels,
                    ))

        warnings = check_codec_supported(codec_video, codec_audio)
        for w in warnings:
            logger.warn("codec_warning", file_path=file_path, warning=w)

        if has_displaymatrix and rotation == 0:
            # 渲染层会关掉 ffmpeg 的自动旋转，元数据读不到就意味着画面方向不可信
            logger.warn(
                "ffmpeg_rotation_unparsed",
                file_path=file_path,
                hint="检测到 displaymatrix 但未解析出角度，输出方向可能需要人工核对",
            )

        return MediaProbe(
            file_path=file_path,
            duration_us=duration_us,
            width=width,
            height=height,
            fps=fps,
            codec_video=codec_video,
            codec_audio=codec_audio,
            rotation=rotation,
            is_vfr=is_vfr,
            time_base=time_base,
            tracks=tracks,
            bit_rate=bit_rate,
        )

    def _parse_probe_result(self, data: dict[str, Any], file_path: str) -> MediaProbe:
        format_info = data.get("format", {})
        streams = data.get("streams", [])

        duration_str = format_info.get("duration", "0")
        duration_us = int(float(duration_str) * 1_000_000) if duration_str else 0

        tracks: list[MediaTrack] = []
        video_stream: dict[str, Any] | None = None
        audio_stream: dict[str, Any] | None = None

        for stream in streams:
            track = self._parse_stream(stream)
            tracks.append(track)
            if track.codec_type == "video" and video_stream is None:
                video_stream = stream
            elif track.codec_type == "audio" and audio_stream is None:
                audio_stream = stream

        width = int(video_stream.get("width", 0)) if video_stream else None
        height = int(video_stream.get("height", 0)) if video_stream else None
        codec_video = video_stream.get("codec_name") if video_stream else None
        codec_audio = audio_stream.get("codec_name") if audio_stream else None

        fps = self._parse_fps(video_stream) if video_stream else None
        rotation = self._parse_rotation(video_stream) if video_stream else 0
        is_vfr = self._detect_vfr(video_stream) if video_stream else False
        time_base = video_stream.get("time_base") if video_stream else None

        bit_rate_str = format_info.get("bit_rate")
        bit_rate = int(bit_rate_str) if bit_rate_str else None

        warnings = check_codec_supported(codec_video, codec_audio)
        for w in warnings:
            logger.warn("codec_warning", file_path=file_path, warning=w)

        return MediaProbe(
            file_path=file_path,
            duration_us=duration_us,
            width=width or None,
            height=height or None,
            fps=fps,
            codec_video=codec_video,
            codec_audio=codec_audio,
            rotation=rotation,
            is_vfr=is_vfr,
            time_base=time_base,
            tracks=tracks,
            bit_rate=bit_rate,
        )

    def _parse_stream(self, stream: dict[str, Any]) -> MediaTrack:
        codec_type = stream.get("codec_type", "unknown")
        codec_name = stream.get("codec_name", "unknown")
        index = int(stream.get("index", 0))

        duration_str = stream.get("duration")
        duration_us = int(float(duration_str) * 1_000_000) if duration_str else None

        fps = self._parse_fps(stream) if codec_type == "video" else None

        return MediaTrack(
            index=index,
            codec_type=codec_type,
            codec_name=codec_name,
            channels=int(stream["channels"]) if "channels" in stream else None,
            sample_rate=int(stream["sample_rate"]) if "sample_rate" in stream else None,
            width=int(stream["width"]) if "width" in stream else None,
            height=int(stream["height"]) if "height" in stream else None,
            fps=fps,
            time_base=stream.get("time_base"),
            duration_us=duration_us,
        )

    def _parse_fps(self, stream: dict[str, Any]) -> float | None:
        r_frame_rate = stream.get("r_frame_rate", "0/1")
        match = _FPS_PATTERN.match(r_frame_rate)
        if not match:
            return None
        num, den = int(match.group(1)), int(match.group(2))
        return num / den if den > 0 else 0.0

    def _parse_rotation(self, stream: dict[str, Any]) -> int:
        side_data = stream.get("side_data_list", [])
        for sd in side_data:
            if "rotation" in sd:
                try:
                    return _normalize_rotation(float(sd["rotation"]))
                except (TypeError, ValueError):
                    logger.warn("rotation_side_data_unparsable", value=sd.get("rotation"))
                    return 0

        tags = stream.get("tags", {})
        rotate = tags.get("rotate")
        if rotate:
            try:
                return _normalize_rotation(float(rotate))
            except (TypeError, ValueError):
                logger.warn("rotation_tag_unparsable", value=rotate)
        return 0

    def _detect_vfr(self, stream: dict[str, Any]) -> bool:
        r_frame_rate = stream.get("r_frame_rate", "")
        avg_frame_rate = stream.get("avg_frame_rate", "")
        if r_frame_rate and avg_frame_rate and r_frame_rate != avg_frame_rate:
            logger.warn("vfr_detected", r_frame_rate=r_frame_rate, avg_frame_rate=avg_frame_rate)
            return True
        return False

    async def run_command(
        self, argv: list[str], timeout_seconds: float | None = None
    ) -> tuple[int, str, str]:
        """执行一条 FFmpeg 命令原语（供渲染执行器按步调用）。"""
        if not argv:
            raise FFmpegError("命令为空")
        return await self._run_subprocess(argv, timeout_seconds=timeout_seconds or self._timeout)

    async def render(
        self, plan: Any, on_step: Any = None
    ) -> Any:
        """顺序执行 RenderPlan 的每一步。

        约定：steps[*].args 不含可执行文件本身，由这里统一前置 self.ffmpeg_path。
        任一步非 0 退出即失败，失败时清理已产生的中间文件由调用方负责。
        """
        from app.models.enums import JobStatus
        from app.models.render_plan import RenderJob

        steps = list(getattr(plan, "ffmpeg_steps", []) or [])
        if not steps:
            raise FFmpegError("渲染计划不含任何步骤")

        job = RenderJob(plan_id=plan.plan_id, status=JobStatus.RUNNING, started_at=datetime.now(UTC))
        total = len(steps)

        for index, step in enumerate(steps):
            argv = [self.ffmpeg_path, "-hide_banner", "-y", *step.args]
            returncode, _stdout, stderr = await self.run_command(argv)
            if returncode != 0:
                job.status = JobStatus.FAILED
                job.error_message = f"步骤 {step.step_index} 失败: {stderr[-400:]}"
                job.finished_at = datetime.now(UTC)
                logger.error(
                    "render_step_failed",
                    plan_id=str(plan.plan_id),
                    step_index=step.step_index,
                    returncode=returncode,
                )
                raise FFmpegError(job.error_message)

            if on_step is not None:
                await on_step(index + 1, total)
            job.progress = (index + 1) / total

        job.status = JobStatus.COMPLETED
        job.progress = 1.0
        job.output_path = steps[-1].output_file
        job.finished_at = datetime.now(UTC)
        logger.info("render_completed", plan_id=str(plan.plan_id), steps=total)
        return job

    def get_audio_tracks(self, probe_result: MediaProbe) -> list[MediaTrack]:
        return [t for t in probe_result.tracks if t.codec_type == "audio"]

    async def extract_audio(
        self, input_path: str, track_index: int, output_path: str
    ) -> str:
        path = validate_media_path(input_path)
        out = Path(output_path)
        if str(out.parent) not in (".", ""):
            out.parent.mkdir(parents=True, exist_ok=True)

        argv = [
            self.ffmpeg_path,
            "-y",
            "-i", str(path),
            "-map", f"0:a:{track_index}",
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", "16000",
            "-ac", "1",
            str(out),
        ]
        returncode, _, stderr = await self._run_subprocess(argv, timeout_seconds=self._timeout)

        if returncode != 0:
            raise FFmpegError(f"音轨提取失败: {stderr[:200]}")

        logger.info(
            "audio_extracted", input_path=str(path), output_path=str(out), track_index=track_index
        )
        return str(out)
