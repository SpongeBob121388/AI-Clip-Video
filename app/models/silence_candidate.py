"""静音候选模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import DetectionMethod


class SilenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=64, description="候选 ID")
    start_us: int = Field(..., description="起始时间（微秒）", ge=0)
    end_us: int = Field(..., description="结束时间（微秒）", ge=0)
    detection_method: DetectionMethod = Field(..., description="检测方法")
    prev_utterance_text: str = Field(default="", max_length=2000, description="前一语句文本")
    next_utterance_text: str = Field(default="", max_length=2000, description="后一语句文本")
    # dBFS 上限取 0（满刻度），下限 -120 已远低于实用静音底噪
    avg_dbfs: float = Field(..., ge=-120.0, le=0.0, description="平均音量（dBFS）")
    confidence: float = Field(default=0.0, description="置信度 [0.0, 1.0]", ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_time_range(self) -> SilenceCandidate:
        if self.end_us <= self.start_us:
            raise ValueError("end_us 必须大于 start_us")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us
