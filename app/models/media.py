"""媒体探测结果模型 — ffprobe 输出的结构化表示。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

_TIME_BASE_PATTERN = re.compile(r"^\d+/\d+$")


class MediaTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(..., description="流索引", ge=0, le=1000)
    codec_type: str = Field(..., min_length=1, max_length=32, description="流类型 (video/audio)")
    codec_name: str = Field(..., min_length=1, max_length=64, description="编码器名称")
    channels: int | None = Field(default=None, description="音频通道数", ge=1, le=32)
    sample_rate: int | None = Field(default=None, description="采样率", ge=1)
    width: int | None = Field(default=None, description="视频宽度", ge=1)
    height: int | None = Field(default=None, description="视频高度", ge=1)
    fps: float | None = Field(default=None, description="帧率", gt=0.0, le=1000.0)
    time_base: str | None = Field(default=None, max_length=32, description="时间基 (如 1/1000)")
    duration_us: int | None = Field(default=None, description="流时长（微秒）", ge=0)

    @field_validator("time_base")
    @classmethod
    def validate_time_base(cls, v: str | None) -> str | None:
        if v is not None and not _TIME_BASE_PATTERN.match(v):
            raise ValueError(f"time_base 必须为 num/den 格式，得到: {v}")
        return v


class MediaProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=4096, description="文件路径")
    duration_us: int = Field(..., description="总时长（微秒）", ge=0)
    width: int | None = Field(default=None, description="视频宽度", ge=1)
    height: int | None = Field(default=None, description="视频高度", ge=1)
    fps: float | None = Field(default=None, description="帧率", gt=0.0, le=1000.0)
    codec_video: str | None = Field(default=None, max_length=64, description="视频编码器")
    codec_audio: str | None = Field(default=None, max_length=64, description="音频编码器")
    rotation: int = Field(default=0, description="旋转角度 (0/90/180/270)")
    is_vfr: bool = Field(default=False, description="是否可变帧率")
    time_base: str | None = Field(default=None, max_length=32, description="时间基")
    tracks: list[MediaTrack] = Field(default_factory=list, max_length=64, description="流列表")
    bit_rate: int | None = Field(default=None, description="总比特率", ge=0)

    @field_validator("rotation")
    @classmethod
    def validate_rotation(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError("rotation 必须为 0/90/180/270")
        return v

    @field_validator("time_base")
    @classmethod
    def validate_time_base(cls, v: str | None) -> str | None:
        if v is not None and not _TIME_BASE_PATTERN.match(v):
            raise ValueError(f"time_base 必须为 num/den 格式，得到: {v}")
        return v

    @property
    def is_vertical(self) -> bool:
        if self.width and self.height:
            return self.height > self.width
        return False

    @property
    def has_audio(self) -> bool:
        return any(t.codec_type == "audio" for t in self.tracks)

    @property
    def has_video(self) -> bool:
        return any(t.codec_type == "video" for t in self.tracks)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
