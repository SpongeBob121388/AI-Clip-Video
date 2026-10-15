"""ASR 语音识别结果模型。"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import ASREngine, ModelSize

_MAX_UTTERANCES = 20_000


class WordSpan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    word_id: str = Field(..., min_length=1, max_length=64, description="词 ID，如 w-35")
    start_us: int = Field(..., description="起始时间（微秒）", ge=0)
    end_us: int = Field(..., description="结束时间（微秒）", ge=0)
    text: str = Field(..., max_length=500, description="词文本")
    confidence: float = Field(..., description="置信度 [0.0, 1.0]")

    @field_validator("confidence")
    @classmethod
    def validate_confidence(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence 必须在 [0.0, 1.0] 范围内")
        return v

    @model_validator(mode="after")
    def validate_time_range(self) -> WordSpan:
        if self.end_us <= self.start_us:
            raise ValueError("end_us 必须大于 start_us")
        return self


class Utterance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    utterance_id: str = Field(..., min_length=1, max_length=64, description="语句 ID")
    start_us: int = Field(..., description="起始时间（微秒）", ge=0)
    end_us: int = Field(..., description="结束时间（微秒）", ge=0)
    text: str = Field(..., max_length=5000, description="语句文本")
    words: Annotated[list[WordSpan], Field(max_length=2000)] = Field(
        default_factory=list, description="词列表"
    )

    @model_validator(mode="after")
    def validate_time_range(self) -> Utterance:
        if self.end_us <= self.start_us:
            raise ValueError("end_us 必须大于 start_us")
        return self

    @property
    def time_key(self) -> str:
        """人工文案的定位键。

        不用 utterance_id：重跑识别后整套 id 都可能变，时间区间至少还稳定。
        注意这是 property，不进 `model_dump`，因此不影响已存的转录载荷格式。
        """
        return f"{self.start_us}-{self.end_us}"


class ASRResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engine: ASREngine = Field(..., description="ASR 引擎")
    language: str = Field(default="zh", min_length=2, max_length=16, description="语言代码")
    model_size: ModelSize = Field(default=ModelSize.MEDIUM, description="模型大小")
    utterances: Annotated[list[Utterance], Field(max_length=_MAX_UTTERANCES)] = Field(
        default_factory=list, description="语句列表"
    )
    total_duration_us: int = Field(default=0, description="总时长（微秒）", ge=0)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ASRResult:
        return cls.model_validate(data)


def apply_text_overrides(
    result: ASRResult, overrides: dict[str, str]
) -> tuple[ASRResult, list[str]]:
    """套用人工改写的文案，返回 (结果副本, 未能套用的键)。

    原始转录不被修改，重新识别一次就能拿回机器文本；对不上的键必须回传而不是静默忽略，
    否则用户改好的字在重跑识别后凭空消失，连原因都查不到。
    """
    if not overrides:
        return result, []
    remaining = dict(overrides)
    utterances = []
    for utterance in result.utterances:
        text = remaining.pop(utterance.time_key, None)
        utterances.append(
            utterance if text is None else utterance.model_copy(update={"text": text})
        )
    return result.model_copy(update={"utterances": utterances}), sorted(remaining)
