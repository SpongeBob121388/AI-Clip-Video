"""RenderPlan 与 RenderJob 模型 — 渲染计划与渲染任务。

预览和最终导出共用同一份 RenderPlan，确保所见即所得。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import AspectRatio, JobStatus, LayoutMode, Resolution, SubtitleMode
from app.models.subtitle_config import SubtitleConfig

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# CRF 的可调区间：低于 14 体积暴涨而肉眼无感，高于 35 字幕边缘开始糊
CRF_MIN, CRF_MAX = 14, 35
# 口播投稿常用档；None（跟随源）由前端单独表示，不在这里列
FPS_OPTIONS: tuple[int, ...] = (25, 30, 60)


class RenderPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: Resolution = Field(default=Resolution.P1080, description="输出分辨率")
    subtitle_mode: SubtitleMode = Field(
        default=SubtitleMode.BURN_IN, description="字幕模式"
    )
    subtitle_config: SubtitleConfig | None = Field(
        default=None, description="字幕配置（subtitle_mode=none 时为空）"
    )
    aspect_ratio: AspectRatio = Field(
        default=AspectRatio.VERTICAL, description="输出宽高比"
    )
    layout_mode: LayoutMode = Field(
        default=LayoutMode.BLUR_BACKGROUND, description="画幅布局模式"
    )
    preview_quality: bool = Field(default=False, description="是否预览质量（降分辨率）")
    fps: int | None = Field(
        default=None,
        ge=1,
        le=120,
        description="输出帧率；None 表示跟随源素材（不做 CFR 归一）",
    )
    crf: int = Field(
        default=18,
        ge=CRF_MIN,
        le=CRF_MAX,
        description="导出的 x264 CRF；预览恒用快速档常量，不受此值影响",
    )
    audio_track_index: int | None = Field(
        default=None,
        ge=0,
        le=31,
        description="输出用第几条音轨（0 起的相对序号）；None 取第一条",
    )

    @field_validator("subtitle_config")
    @classmethod
    def validate_subtitle(cls, v: SubtitleConfig | None, info: Any) -> SubtitleConfig | None:
        mode = info.data.get("subtitle_mode")
        if mode == SubtitleMode.NONE and v is not None:
            return None
        return v


class FFmpegStep(BaseModel):
    """单步 FFmpeg 调用。args 会被原样传给 create_subprocess_exec（无 shell），
    因此这里限制形状：禁止空串与控制字符（换行会在 concat 清单里伪造额外条目）。"""

    model_config = ConfigDict(extra="forbid")

    step_index: int = Field(..., description="步骤序号", ge=0)
    description: str = Field(..., min_length=1, max_length=500, description="步骤描述")
    args: Annotated[list[str], Field(min_length=1, max_length=64)] = Field(
        ..., description="FFmpeg 参数列表"
    )
    input_files: Annotated[list[str], Field(max_length=4096)] = Field(
        default_factory=list, description="输入文件"
    )
    output_file: str = Field(..., min_length=1, max_length=4096, description="输出文件")

    @field_validator("args")
    @classmethod
    def validate_args(cls, v: list[str]) -> list[str]:
        for item in v:
            if not item:
                raise ValueError("args 不可包含空串")
            if len(item) > 4096:
                raise ValueError("args 单项过长")
            if any(ord(ch) < 0x20 for ch in item):
                raise ValueError("args 不可包含控制字符")
        return v


class RenderPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: UUID = Field(default_factory=uuid4, description="渲染计划 ID")
    project_revision_id: UUID = Field(..., description="项目版本 ID")
    preset: RenderPreset = Field(..., description="渲染预设")
    ffmpeg_steps: Annotated[list[FFmpegStep], Field(max_length=4096)] = Field(
        default_factory=list, description="FFmpeg 执行步骤"
    )
    plan_hash: str = Field(..., description="计划哈希（用于缓存和一致性校验）")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="创建时间"
    )
    source_intervals: Annotated[list[dict[str, int]], Field(max_length=4096)] = Field(
        default_factory=list, description="源区间列表 [{start_us, end_us}]"
    )

    @field_validator("plan_hash")
    @classmethod
    def validate_hash(cls, v: str) -> str:
        if not _HASH_PATTERN.match(v):
            raise ValueError("plan_hash 必须为 64 位十六进制")
        return v

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RenderJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(default_factory=uuid4, description="渲染任务 ID")
    plan_id: UUID = Field(..., description="渲染计划 ID")
    status: JobStatus = Field(default=JobStatus.PENDING, description="任务状态")
    progress: float = Field(default=0.0, description="进度 [0.0, 1.0]", ge=0.0, le=1.0)
    output_path: str | None = Field(default=None, description="输出文件路径")
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="完成时间")
    error_message: str | None = Field(default=None, description="错误信息")
    is_preview: bool = Field(default=False, description="是否预览任务")

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
