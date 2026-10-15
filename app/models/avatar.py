"""数字人视频合成请求模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import SynthesisStatus

_MAX_SCRIPT_CHARS = 20_000
_MAX_ID_LEN = 200
_MAX_PATH_LEN = 4096
_MAX_ERROR_LEN = 2000


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speed: float = Field(default=1.0, description="语速 [0.5, 2.0]", ge=0.5, le=2.0)
    pitch: float = Field(default=0.0, description="音高 [-12.0, 12.0]", ge=-12.0, le=12.0)
    voice_id: str = Field(default="default", max_length=_MAX_ID_LEN, description="音色 ID")


class AvatarSynthesisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4, description="请求 ID")
    text_script: str = Field(
        ..., max_length=_MAX_SCRIPT_CHARS, description="合成文本脚本", min_length=1
    )
    avatar_id: str = Field(..., min_length=1, max_length=_MAX_ID_LEN, description="数字人形象 ID")
    voice_config: VoiceConfig = Field(default_factory=VoiceConfig, description="语音配置")
    status: SynthesisStatus = Field(
        default=SynthesisStatus.PENDING, description="合成状态"
    )
    output_video_path: str | None = Field(
        default=None, max_length=_MAX_PATH_LEN, description="输出视频路径"
    )
    background_image_path: str | None = Field(
        default=None, max_length=_MAX_PATH_LEN, description="背景图片路径"
    )

    @field_validator("text_script")
    @classmethod
    def validate_script(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text_script 不可为空或纯空白")
        return v

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class SynthesisJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID = Field(default_factory=uuid4, description="合成任务 ID")
    request_id: UUID = Field(..., description="关联请求 ID")
    status: SynthesisStatus = Field(
        default=SynthesisStatus.PENDING, description="合成状态"
    )
    progress: float = Field(default=0.0, description="进度 [0.0, 1.0]", ge=0.0, le=1.0)
    output_path: str | None = Field(
        default=None, max_length=_MAX_PATH_LEN, description="输出路径"
    )
    started_at: datetime | None = Field(default=None, description="开始时间")
    finished_at: datetime | None = Field(default=None, description="完成时间")
    error_message: str | None = Field(
        default=None, max_length=_MAX_ERROR_LEN, description="错误信息"
    )

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
