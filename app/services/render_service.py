"""Render Service — RenderPlan 生成与执行。

预览与导出共用同一份 RenderPlan，确保所见即所得。

设计要点（修正旧实现的四个问题）：
1. 单趟 filtergraph：用 trim/atrim + concat 在一条形如 `-filter_complex` 的图里完成剪切，
   而不是"逐段 -c copy 抽取 + concat 清单"。逐段 copy 会按上一个关键帧切，真实时长与
   微秒级计划不一致，字幕会随时间累计漂移；而逐段重编码再合并等于编码两次。
2. 计划里的 concat_list.txt 从未被任何代码写出，因此计划本身不可执行；现在 argv 完整可跑。
3. 中间段文件不再落在 CWD 且不再残留；产物用 ffprobe 校验时长后才宣告成功。
4. 字幕路径由调用方在工作目录内生成（受控文件名），并对 filter 里的路径做转义，
   避免片名里的单引号把任意滤镜文本注入 filtergraph。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from app.config import get_settings
from app.models.edl import EDL, OutputInterval
from app.models.enums import AspectRatio, JobStatus, LayoutMode, Resolution, SubtitleMode
from app.models.render_plan import FFmpegStep, RenderJob, RenderPlan, RenderPreset
from app.providers.ffmpeg.runner import FFmpegError, FFmpegRunner
from app.services.edl_service import EDLService
from app.utils.logging import get_logger
from app.utils.path_validator import (
    validate_media_path,
    validate_output_path,
    validate_subtitle_path,
)

logger = get_logger("services.render_service")

_RESOLUTION_MAP: dict[Resolution, tuple[int, int]] = {
    Resolution.P720: (1280, 720),
    Resolution.P1080: (1920, 1080),
}

_VIDEO_ENCODER = "libx264"
_PIXEL_FORMAT = "yuv420p"
# 预览恒用这一档：预览是快速验证用的，不受 preset.crf 影响
_CRF_PREVIEW = 26
_PRESET_PREVIEW = "veryfast"
_PRESET_FINAL = "medium"
_AUDIO_CODEC = "aac"
_AUDIO_BITRATE = "192k"
_SAMPLE_RATE = "48000"
# 校验容差：取 max(0.5s, 时长 2%)，避免为最后一帧的取整问题误报失败
_DURATION_TOLERANCE_US = 500_000
_DURATION_TOLERANCE_RATIO = 0.02


class RenderServiceError(RuntimeError):
    pass


class RenderCancelledError(RenderServiceError):
    pass


class RenderVerificationError(RenderServiceError):
    pass


@dataclass(frozen=True)
class RenderOutcome:
    output_path: Path
    duration_us: int
    width: int | None
    height: int | None
    size_bytes: int
    plan_hash: str
    expected_duration_us: int


class RenderService:
    """渲染服务 — 生成 RenderPlan（预览/导出共用）。"""

    @staticmethod
    def generate_render_plan(
        edl: EDL,
        preset: RenderPreset,
        project_revision_id: UUID,
        source_file_path: str = "",
        subtitle_file_path: str = "",
        output_file_path: str = "output.mp4",
        has_audio: bool = True,
        plan_id: UUID | None = None,
    ) -> RenderPlan:
        """生成可执行的 RenderPlan。"""
        output_intervals = EDLService.compute_output_intervals(edl)
        ffmpeg_steps, filtergraph = RenderService._build_ffmpeg_steps(
            output_intervals,
            preset,
            source_file_path,
            subtitle_file_path,
            output_file_path,
            has_audio,
            edl.source.rotation,
        )
        source_intervals = [
            {"start_us": iv.source_start_us, "end_us": iv.source_start_us + iv.duration_us}
            for iv in output_intervals
        ]
        plan_hash = RenderService._compute_hash(
            preset=preset,
            source_intervals=source_intervals,
            source_file=source_file_path,
            output_file=output_file_path,
            subtitle_file=subtitle_file_path,
            filtergraph=filtergraph,
            has_audio=has_audio,
        )

        plan = RenderPlan(
            plan_id=plan_id or uuid4(),
            project_revision_id=project_revision_id,
            preset=preset,
            ffmpeg_steps=ffmpeg_steps,
            plan_hash=plan_hash,
            source_intervals=source_intervals,
        )
        logger.info(
            "render_plan_generated",
            plan_id=str(plan.plan_id),
            plan_hash=plan_hash[:12],
            steps=len(ffmpeg_steps),
            intervals=len(output_intervals),
            expected_duration_us=RenderService.expected_output_duration_us(output_intervals),
        )
        return plan

    @staticmethod
    def expected_output_duration_us(intervals: list[OutputInterval]) -> int:
        return sum(iv.duration_us for iv in intervals)

    @staticmethod
    def _build_ffmpeg_steps(
        output_intervals: list[OutputInterval],
        preset: RenderPreset,
        source_file: str,
        subtitle_file: str,
        output_file: str,
        has_audio: bool = True,
        rotation: int = 0,
    ) -> tuple[list[FFmpegStep], str]:
        if not source_file:
            raise RenderServiceError("源文件路径为空，无法生成渲染计划")
        if not output_file:
            raise RenderServiceError("输出文件路径为空，无法生成渲染计划")
        if not output_intervals:
            raise RenderServiceError("没有保留区间，渲染结果为空")

        burn_subtitles = (
            preset.subtitle_mode == SubtitleMode.BURN_IN and bool(subtitle_file)
        )
        filtergraph = RenderService._build_filtergraph(
            output_intervals,
            preset,
            subtitle_file if burn_subtitles else "",
            has_audio,
            rotation,
        )

        args: list[str] = [
            # FFmpeg 只在输入流被单个滤镜消费时才自动旋转；多段 trim 会让它退化成
            # "像素不转、只把 displaymatrix 抄到输出"，于是竖屏素材在播放器里横倒。
            # 这里关掉自动旋转并按探测到的角度显式 transpose，输出方向与段数无关。
            "-noautorotate",
            "-display_rotation:v:0", "0",
            "-i", source_file,
            "-filter_complex", filtergraph,
            "-map", "[outv]",
        ]
        if has_audio:
            args += ["-map", "[outa]"]
        args += [
            "-c:v", _VIDEO_ENCODER,
            "-preset", _PRESET_PREVIEW if preset.preview_quality else _PRESET_FINAL,
            "-crf", str(_CRF_PREVIEW if preset.preview_quality else preset.crf),
            "-pix_fmt", _PIXEL_FORMAT,
        ]
        if has_audio:
            args += [
                "-c:a", _AUDIO_CODEC,
                "-b:a", _AUDIO_BITRATE,
                "-ar", _SAMPLE_RATE,
                "-ac", "2",
            ]
        args += ["-movflags", "+faststart", output_file]

        step = FFmpegStep(
            step_index=0,
            description="剪切保留区间并合成输出",
            args=args,
            input_files=[source_file] + ([subtitle_file] if burn_subtitles else []),
            output_file=output_file,
        )
        return [step], filtergraph

    @staticmethod
    def _build_filtergraph(
        intervals: list[OutputInterval],
        preset: RenderPreset,
        subtitle_file: str,
        has_audio: bool,
        rotation: int = 0,
    ) -> str:
        """构造单趟 filtergraph：旋转 → trim → concat → 画幅/缩放 → 烧录字幕 → 输出标签。"""
        chains: list[str] = []

        rot_filter = RenderService._rotation_filter(rotation)
        audio_in = f"[0:a:{preset.audio_track_index or 0}]"
        if rot_filter:
            if len(intervals) == 1:
                video_labels = ["[rv0]"]
                chains.append(f"[0:v]{rot_filter}[rv0]")
            else:
                # 旋转只做一次：split 出与段数等量的分支，避免每段重复 transpose
                video_labels = [f"[rv{i}]" for i in range(len(intervals))]
                chains.append(
                    f"[0:v]{rot_filter},split={len(intervals)}{''.join(video_labels)}"
                )
        else:
            video_labels = ["[0:v]"] * len(intervals)

        for index, iv in enumerate(intervals):
            start = iv.source_start_us / 1_000_000
            end = (iv.source_start_us + iv.duration_us) / 1_000_000
            chains.append(
                f"{video_labels[index]}trim=start={start:.6f}:end={end:.6f},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            if has_audio:
                chains.append(
                    f"{audio_in}atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS[a{index}]"
                )

        # 统一走 concat（包括只有一段的情况），避免两套标签分支各自漂移
        inputs = "".join(
            f"[v{i}]" + (f"[a{i}]" if has_audio else "") for i in range(len(intervals))
        )
        if has_audio:
            chains.append(f"{inputs}concat=n={len(intervals)}:v=1:a=1[cv][ca]")
            audio_out_label: str | None = "[ca]"
        else:
            chains.append(f"{inputs}concat=n={len(intervals)}:v=1:a=0[cv]")
            audio_out_label = None
        current = "[cv]"

        layout_chain, current = RenderService._layout_filters(preset, current)
        chains.extend(layout_chain)

        tail: list[str] = []
        if preset.fps:
            # 用 fps 滤镜而不是 -r：源是 VFR 时 -r 与 filtergraph 的时基语义容易混淆，
            # 放在链尾才是「按目标帧率重采样」的确定做法
            tail.append(f"fps={preset.fps}")
        tail.append("format=yuv420p")
        if subtitle_file:
            tail.append(
                f"subtitles=filename='{RenderService._filter_escape_path(subtitle_file)}'"
            )
        chains.append(f"{current}{','.join(tail)}[outv]")

        if has_audio and audio_out_label:
            chains.append(f"{audio_out_label}anull[outa]")

        return ";".join(chains)

    @staticmethod
    def target_canvas(preset: RenderPreset) -> tuple[int, int]:
        """公开入口：输出画布尺寸 (宽, 高)，供字幕 PlayRes 等调用方使用。"""
        return RenderService._target_canvas(preset)

    @staticmethod
    def _target_canvas(preset: RenderPreset) -> tuple[int, int]:
        """输出画布尺寸 (宽, 高)。竖屏时高 > 宽，且保持偶数以适配 yuv420p。"""
        base = _RESOLUTION_MAP.get(preset.resolution) or (1920, 1080)
        long_side, short_side = max(base), min(base)
        if preset.preview_quality:
            # 先减半再向下取偶数：//2*2 会把奇数放大，得到没减半的尺寸
            long_side = (long_side // 2) - (long_side // 2) % 2
            short_side = (short_side // 2) - (short_side // 2) % 2
        if preset.aspect_ratio == AspectRatio.VERTICAL:
            return short_side, long_side
        return long_side, short_side

    # 源素材常见 90/270 旋转：滤镜方向已与 FFmpeg 自动旋转逐像素比对过（SSIM=1.0）
    _ROTATION_FILTERS: dict[int, str] = {
        90: "transpose=2",
        180: "hflip,vflip",
        270: "transpose=1",
    }

    @staticmethod
    def _rotation_filter(rotation: int) -> str:
        """把源的正交旋转角翻译成等效滤镜；0 或未知角度返回空串。"""
        return RenderService._ROTATION_FILTERS.get(rotation % 360, "")

    @staticmethod
    def _layout_filters(preset: RenderPreset, in_label: str) -> tuple[list[str], str]:
        """把画面归一到目标画布：智能裁切填满，或模糊背景填充。

        两种布局都自己产出最终尺寸，主链不再另加 scale；等比缩放的结果可能是奇数
        （如 360x203），那会让 libx264 在 "Error while opening encoder" 处直接失败，
        所以缩放一律带 force_divisible_by=2。选「源分辨率」时完全不改画幅。
        """
        if preset.resolution not in _RESOLUTION_MAP:
            # 「源分辨率」不改画幅，但 yuv420p 仍要求偶数边长：奇数宽高会让 libx264
            # 报 "width not divisible by 2" 并写出空文件，这里只取偶、不改变观感尺寸
            return [f"{in_label}scale=trunc(iw/2)*2:trunc(ih/2)*2[laid]"], "[laid]"

        width, height = RenderService._target_canvas(preset)
        out_label = "[laid]"

        if preset.layout_mode == LayoutMode.SMART_CROP:
            # 居中裁剪填满画布：只缩放不裁剪的话输出尺寸会随源素材漂移
            return [
                f"{in_label}scale={width}:{height}:force_original_aspect_ratio=increase"
                ":force_divisible_by=2"
                f",crop={width}:{height}{out_label}"
            ], out_label
        chains = [
            f"{in_label}split=2[fg_src][bg_src]",
            (
                f"[bg_src]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},boxblur=20:5[bg]"
            ),
            f"[fg_src]scale={width}:{height}:force_original_aspect_ratio=decrease[fg]",
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2:eof_action=pass{out_label}",
        ]
        return chains, out_label

    @staticmethod
    def _filter_escape_path(path: str) -> str:
        """转义 filter 里的文件路径：反斜杠→正斜杠，冒号与单引号需转义。"""
        normalized = path.replace("\\", "/")
        return (
            normalized.replace(":", r"\:")
            .replace("'", r"\'")
        )

    @staticmethod
    def _compute_hash(
        preset: RenderPreset,
        source_intervals: list[dict[str, int]],
        source_file: str,
        output_file: str,
        subtitle_file: str,
        filtergraph: str,
        has_audio: bool,
    ) -> str:
        """对计划的语义内容求哈希。

        刻意不包含临时目录等易变绝对路径：否则同一 EDL 每次生成的哈希都不同，
        预览缓存永远命中不了。
        """
        content = {
            "preset": preset.model_dump(mode="json"),
            "source_intervals": source_intervals,
            "source_file": str(Path(source_file).resolve()) if source_file else "",
            "output_name": Path(output_file).name,
            "subtitle_name": Path(subtitle_file).name if subtitle_file else "",
            "filtergraph": filtergraph,
            "has_audio": has_audio,
            "encoder": {
                "codec": _VIDEO_ENCODER,
                "crf": _CRF_PREVIEW if preset.preview_quality else preset.crf,
                "preset": _PRESET_PREVIEW if preset.preview_quality else _PRESET_FINAL,
            },
        }
        payload = json.dumps(content, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def generate_preview_plan(
        edl: EDL,
        project_revision_id: UUID,
        source_file_path: str = "",
        subtitle_file_path: str = "",
        output_file_path: str = "preview.mp4",
        has_audio: bool = True,
    ) -> RenderPlan:
        """生成预览 RenderPlan（降分辨率 + 快速编码）。"""
        preset = RenderPreset(
            resolution=Resolution.P720,
            subtitle_mode=SubtitleMode.BURN_IN if subtitle_file_path else SubtitleMode.NONE,
            aspect_ratio=AspectRatio.VERTICAL,
            layout_mode=LayoutMode.BLUR_BACKGROUND,
            preview_quality=True,
        )
        return RenderService.generate_render_plan(
            edl,
            preset,
            project_revision_id,
            source_file_path,
            subtitle_file_path,
            output_file_path,
            has_audio,
        )

    @staticmethod
    def generate_export_plan(
        edl: EDL,
        project_revision_id: UUID,
        preset: RenderPreset,
        source_file_path: str = "",
        subtitle_file_path: str = "",
        output_file_path: str = "export.mp4",
        has_audio: bool = True,
    ) -> RenderPlan:
        """生成导出 RenderPlan（1080p 或源分辨率）。"""
        return RenderService.generate_render_plan(
            edl,
            preset,
            project_revision_id,
            source_file_path,
            subtitle_file_path,
            output_file_path,
            has_audio,
        )

    @staticmethod
    def check_disk_space(
        output_dir: str, estimated_size_bytes: int, available_bytes: int
    ) -> None:
        """磁盘空间预检。"""
        if available_bytes < estimated_size_bytes:
            needed_gb = estimated_size_bytes / (1024**3)
            available_gb = available_bytes / (1024**3)
            raise RenderServiceError(
                f"磁盘空间不足，需要约 {needed_gb:.1f} GB，当前可用 {available_gb:.1f} GB"
            )

    @staticmethod
    def check_output_path(output_path: str, source_path: str) -> None:
        """导出路径预检：禁止与源文件相同（解析后比较，大小写与写法差异也算）。"""
        if not output_path or not source_path:
            return
        out = Path(output_path).resolve()
        src = Path(source_path).resolve()
        if _same_path(out, src):
            raise RenderServiceError("导出路径不能与源文件路径相同")

    @staticmethod
    def estimate_output_size(
        duration_us: int, resolution: Resolution, bitrate_mbps: float = 8.0
    ) -> int:
        """估算输出文件大小（字节）。"""
        duration_sec = duration_us / 1_000_000
        return int(duration_sec * bitrate_mbps * 1_000_000 / 8)

    @staticmethod
    def generate_default_filename(source_filename: str, local_timezone: bool = True) -> str:
        """生成默认导出文件名：{源文件名}_edited_{时间戳}.mp4"""
        stem = Path(source_filename).stem[:80] or "video"
        now = datetime.now().astimezone() if local_timezone else datetime.now(UTC)
        return f"{stem}_edited_{now.strftime('%Y%m%d_%H%M%S')}.mp4"

    @staticmethod
    def build_metadata_args(
        project_id: str, plan_hash: str, software_version: str
    ) -> list[str]:
        """构建 FFmpeg -metadata 参数。"""
        return [
            "-metadata", f"project_id={_sanitize_meta(project_id)}",
            "-metadata", f"renderplan_hash={_sanitize_meta(plan_hash)}",
            "-metadata", f"software_version={_sanitize_meta(software_version)}",
        ]


def _same_path(a: Path, b: Path) -> bool:
    if a == b:
        return True
    if Path(str(a).lower()) == Path(str(b).lower()):
        return True
    if a.exists() and b.exists():
        try:
            return (a.stat().st_dev, a.stat().st_ino) == (b.stat().st_dev, b.stat().st_ino)
        except OSError:
            return False
    return False


def _sanitize_meta(value: str) -> str:
    """metadata 里避免换行与等号破坏 -metadata 语法。"""
    return str(value).replace("=", "_").replace("\n", " ").replace("\r", " ")[:200]


class RenderExecutor:
    """执行 RenderPlan 并校验产物。"""

    def __init__(
        self,
        runner: FFmpegRunner | None = None,
        work_root: Path | str | None = None,
        keep_debug_files: bool = False,
    ) -> None:
        self.runner = runner or FFmpegRunner()
        self.work_root = Path(work_root) if work_root else get_settings().render_work_dir
        self.keep_debug_files = keep_debug_files

    def subtitle_path_for(self, plan_id: UUID | str) -> Path:
        plan_dir = self._plan_dir(plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir / "subtitles.ass"

    def output_path_for(self, plan_id: UUID | str, filename: str) -> Path:
        plan_dir = self._plan_dir(plan_id)
        plan_dir.mkdir(parents=True, exist_ok=True)
        return plan_dir / filename

    def _plan_dir(self, plan_id: UUID | str) -> Path:
        return self.work_root / str(plan_id)

    async def write_subtitle_file(self, plan_id: UUID | str, content: str) -> Path:
        """把字幕内容写进工作目录下的受控文件名。

        只有我们自己生成的路径会进入 filtergraph，片名里的引号再也不会影响滤镜解析。
        """
        path = self.subtitle_path_for(plan_id)
        path.write_text(content, encoding="utf-8")
        return path

    async def execute(
        self,
        plan: RenderPlan,
        *,
        source_path: str | None = None,
        output_path: str | None = None,
        subtitle_path: str | None = None,
        on_progress: Any = None,
        should_stop: Any = None,
    ) -> RenderOutcome:
        """按计划执行渲染，返回经过 ffprobe 校验的结果。

        plan 里的 argv 是完整的，这里只把可能变化的路径（源/输出/字幕）替换成调用方
        最终确定的绝对路径，其余参数保持哈希一致。
        """
        if not plan.ffmpeg_steps:
            raise RenderServiceError("渲染计划不含任何步骤")

        step = plan.ffmpeg_steps[-1]
        expected_duration_us = sum(
            int(item["end_us"]) - int(item["start_us"]) for item in plan.source_intervals
        ) or None

        # 执行边界上做路径校验：计划本身可能是外部提交的
        resolved_source = validate_media_path(source_path) if source_path else None
        resolved_subtitle = validate_subtitle_path(subtitle_path) if subtitle_path else None
        argv = self._resolve_argv(
            step.args,
            source_path=str(resolved_source) if resolved_source else None,
            output_path=None,
            subtitle_path=str(resolved_subtitle) if resolved_subtitle else None,
        )
        raw_output = output_path or argv[-1]
        final_output = validate_output_path(
            str(raw_output),
            None,
            source_path=str(resolved_source) if resolved_source else None,
        )
        argv[-1] = str(final_output)
        final_output.parent.mkdir(parents=True, exist_ok=True)

        probe_before = None
        if final_output.exists():
            probe_before = final_output.stat().st_mtime_ns

        job = RenderJob(plan_id=plan.plan_id, status=JobStatus.RUNNING, started_at=datetime.now(UTC))
        total = expected_duration_us or 0

        def _on_time(out_time_us: int) -> None:
            if on_progress is None or total <= 0:
                return
            job.progress = max(0.0, min(1.0, out_time_us / total))
            try:
                on_progress(job.progress, f"已渲染 {out_time_us / 1_000_000:.1f}s")
            except Exception as exc:  # noqa: BLE001 - 进度回调不得影响渲染
                logger.warning("progress_callback_failed", error=str(exc))

        argv_with_progress = [
            self.runner.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-progress", "pipe:1",
            *argv,
        ]

        try:
            returncode, stderr_text = await self.runner.run_with_progress(
                argv_with_progress,
                on_time_us=_on_time,
                should_stop=should_stop,
            )
        except FFmpegError as exc:
            job.status = JobStatus.FAILED
            job.error_message = str(exc)[:500]
            job.finished_at = datetime.now(UTC)
            self._cleanup(plan.plan_id)
            if "已取消" in str(exc):
                raise RenderCancelledError(str(exc)) from exc
            raise RenderServiceError(str(exc)) from exc

        if returncode != 0:
            job.status = JobStatus.FAILED
            job.error_message = stderr_text[-500:]
            self._cleanup(plan.plan_id)
            logger.error(
                "render_failed",
                plan_id=str(plan.plan_id),
                returncode=returncode,
                stderr_tail=stderr_text[-300:],
            )
            raise RenderServiceError(f"渲染失败（退出码 {returncode}）")

        if not final_output.exists():
            self._cleanup(plan.plan_id)
            raise RenderVerificationError("渲染命令成功但没有产出文件")
        if probe_before is not None and final_output.stat().st_mtime_ns == probe_before:
            raise RenderVerificationError("输出文件未被本次渲染更新")

        outcome = await self._verify(plan, final_output, expected_duration_us)
        self._cleanup(plan.plan_id)
        return outcome

    def _resolve_argv(
        self,
        args: list[str],
        *,
        source_path: str | None,
        output_path: str | None,
        subtitle_path: str | None,
    ) -> list[str]:
        """把计划里的路径替换成调用方最终确定的绝对路径。

        filtergraph 是单个 argv 元素，字幕路径必须在其中做子串替换，
        整项覆盖会把整条滤镜链抹掉。
        """
        resolved = list(args)
        if not resolved:
            raise RenderServiceError("渲染步骤不含参数")

        if output_path:
            resolved[-1] = str(output_path)

        if source_path:
            for index in range(len(resolved) - 1):
                if resolved[index] == "-i":
                    resolved[index + 1] = str(source_path)
                    break

        if subtitle_path:
            escaped = RenderService._filter_escape_path(str(subtitle_path))
            replacement = f"subtitles=filename='{escaped}'"
            for index, item in enumerate(resolved):
                if "subtitles=filename=" in item:
                    # 用函数式替换：转义后的反斜杠不参与 re.sub 的组引用解析
                    resolved[index] = re.sub(
                        r"subtitles=filename='[^']*'", lambda _m, r=replacement: r, item
                    )
                    break
        return resolved

    async def _verify(
        self,
        plan: RenderPlan,
        output_path: Path,
        expected_duration_us: int | None,
    ) -> RenderOutcome:
        probe = await self.runner.probe(str(output_path))
        size_bytes = output_path.stat().st_size

        if expected_duration_us:
            tolerance = max(
                _DURATION_TOLERANCE_US,
                int(expected_duration_us * _DURATION_TOLERANCE_RATIO),
            )
            delta = abs(probe.duration_us - expected_duration_us)
            if delta > tolerance:
                raise RenderVerificationError(
                    f"产物时长 {probe.duration_us / 1_000_000:.2f}s 与计划"
                    f" {expected_duration_us / 1_000_000:.2f}s 偏差 {delta / 1_000_000:.2f}s"
                )

        if probe.duration_us <= 0:
            raise RenderVerificationError("产物时长为 0，疑似渲染失败")

        logger.info(
            "render_verified",
            plan_id=str(plan.plan_id),
            duration_us=probe.duration_us,
            size_bytes=size_bytes,
        )
        return RenderOutcome(
            output_path=output_path,
            duration_us=probe.duration_us,
            width=probe.width,
            height=probe.height,
            size_bytes=size_bytes,
            plan_hash=plan.plan_hash,
            expected_duration_us=expected_duration_us or probe.duration_us,
        )

    def _cleanup(self, plan_id: UUID) -> None:
        if self.keep_debug_files:
            return
        plan_dir = self._plan_dir(plan_id)
        if not plan_dir.exists():
            # 成功/失败/取消路径都会调用，重复清理不是错误
            return
        try:
            shutil.rmtree(plan_dir)
        except OSError as exc:
            logger.warning("render_workdir_cleanup_failed", error=str(exc))
