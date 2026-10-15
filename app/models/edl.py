"""EDL（Edit Decision List）领域模型。

时间区间全部使用整数微秒，左闭右开 [start_us, end_us)。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import (
    AspectRatio,
    Decision,
    DecisionSource,
    EditOperationType,
    LayoutMode,
    Resolution,
    SuggestionStatus,
    SuggestionStrength,
    SuggestionType,
)

_MAX_TEXT_LEN = 2000
_MAX_SUGGESTIONS = 20_000
_MAX_WORD_IDS = 5_000


class TimeBase(BaseModel):
    num: int = Field(..., description="分子", ge=1)
    den: int = Field(..., description="分母", ge=1)


class EDLSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uri: str = Field(..., min_length=1, max_length=4096, description="项目相对路径")
    duration_us: int = Field(..., description="源时长（微秒）", ge=0)
    time_base: TimeBase = Field(default_factory=lambda: TimeBase(num=1, den=90000))
    rotation: int = Field(default=0, description="旋转角度（0/90/180/270）")
    is_vfr: bool = Field(default=False, description="是否可变帧率")

    @field_validator("rotation")
    @classmethod
    def validate_rotation(cls, v: int) -> int:
        if v not in (0, 90, 180, 270):
            raise ValueError("rotation 必须为 0/90/180/270")
        return v


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="建议 ID，如 cut-001", min_length=1, max_length=64)
    source_start_us: int = Field(..., description="源起始时间（微秒）", ge=0)
    source_end_us: int = Field(..., description="源结束时间（微秒）", ge=0)
    suggestion_type: SuggestionType = Field(..., description="建议类型")
    decision: Decision = Field(default=Decision.UNCERTAIN, description="决策结果")
    source: DecisionSource = Field(default=DecisionSource.LLM, description="决策来源")
    reason: str = Field(default="", max_length=_MAX_TEXT_LEN, description="建议原因")
    status: SuggestionStatus = Field(default=SuggestionStatus.PENDING, description="确认状态")
    strength: SuggestionStrength = Field(
        default=SuggestionStrength.STANDARD, description="建议强度"
    )
    explanation: str = Field(default="", max_length=_MAX_TEXT_LEN, description="人类可读解释")
    prev_text: str = Field(default="", max_length=_MAX_TEXT_LEN, description="前文上下文")
    next_text: str = Field(default="", max_length=_MAX_TEXT_LEN, description="后文上下文")
    word_ids: Annotated[
        list[str], Field(max_length=_MAX_WORD_IDS)
    ] = Field(default_factory=list, description="关联的词 ID")

    @model_validator(mode="after")
    def validate_time_range(self) -> Suggestion:
        if self.source_end_us <= self.source_start_us:
            raise ValueError("source_end_us 必须大于 source_start_us")
        return self


class EDLOutput(BaseModel):
    aspect_ratio: AspectRatio = Field(default=AspectRatio.VERTICAL, description="输出宽高比")
    resolution: Resolution = Field(default=Resolution.P1080, description="输出分辨率")
    subtitle_preset_id: str = Field(default="default-cn", description="字幕预设 ID")
    layout_mode: LayoutMode = Field(
        default=LayoutMode.BLUR_BACKGROUND, description="画幅布局模式"
    )


class EDL(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, description="Schema 版本", ge=1)
    project_id: UUID = Field(default_factory=uuid4, description="项目 ID")
    source: EDLSource = Field(..., description="源媒体信息")
    suggestions: Annotated[
        list[Suggestion], Field(max_length=_MAX_SUGGESTIONS)
    ] = Field(default_factory=list, description="剪辑建议列表")
    output: EDLOutput = Field(default_factory=EDLOutput, description="输出配置")

    @model_validator(mode="after")
    def validate_suggestions(self) -> EDL:
        sorted_suggestions = sorted(self.suggestions, key=lambda s: s.source_start_us)
        for i in range(1, len(sorted_suggestions)):
            if sorted_suggestions[i].source_start_us < sorted_suggestions[i - 1].source_end_us:
                raise ValueError(
                    f"建议区间重叠: {sorted_suggestions[i - 1].id} 和 {sorted_suggestions[i].id}"
                )
        return self

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> EDL:
        return cls.model_validate(data)

    def accepted_suggestions(self) -> list[Suggestion]:
        return [s for s in self.suggestions if s.status == SuggestionStatus.ACCEPTED]

    def pending_suggestions(self) -> list[Suggestion]:
        return [s for s in self.suggestions if s.status == SuggestionStatus.PENDING]
    def validate_full(self) -> list[str]:
        """结构完整性校验：apply_operation 的唯一守门人，必须覆盖重叠与越界。"""
        errors: list[str] = []
        if self.schema_version != 1:
            errors.append(f"schemaVersion 必须为 1，得到 {self.schema_version}")
        if self.source.duration_us < 0:
            errors.append("source.duration_us 不可为负")

        ordered = sorted(self.suggestions, key=lambda s: s.source_start_us)
        previous: Suggestion | None = None
        for s in ordered:
            if s.source_end_us <= s.source_start_us:
                errors.append(f"建议 {s.id} 时间区间无效")
            if s.source_start_us < 0:
                errors.append(f"建议 {s.id} start_us 不可为负")
            if self.source.duration_us and s.source_end_us > self.source.duration_us:
                errors.append(
                    f"建议 {s.id} end_us={s.source_end_us} 超出源时长 {self.source.duration_us}"
                )
            if previous is not None and s.source_start_us < previous.source_end_us:
                errors.append(f"建议区间重叠: {previous.id} 和 {s.id}")
            previous = s

        ids = [s.id for s in self.suggestions]
        if len(ids) != len(set(ids)):
            errors.append("建议 id 重复")

        return errors


class OutputInterval(BaseModel):
    start_us: int = Field(..., description="输出起始时间（微秒）", ge=0)
    end_us: int = Field(..., description="输出结束时间（微秒）", ge=0)
    source_start_us: int = Field(..., description="对应源起始时间（微秒）", ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> OutputInterval:
        if self.end_us <= self.start_us:
            raise ValueError("end_us 必须大于 start_us")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


class EDLOperation(BaseModel):
    """编辑操作基类。子类以 Literal 判别符收窄 operation_type，
    避免 `EDLOperation(operation_type=<任意字符串>)` 被静默接受。"""

    model_config = ConfigDict(extra="forbid")

    operation_type: EditOperationType = Field(..., description="操作类型")


class AddInterval(EDLOperation):
    operation_type: Literal[EditOperationType.ADD] = EditOperationType.ADD
    source_start_us: int = Field(..., ge=0)
    source_end_us: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> AddInterval:
        if self.source_end_us <= self.source_start_us:
            raise ValueError("source_end_us 必须大于 source_start_us")
        return self


class RemoveInterval(EDLOperation):
    operation_type: Literal[EditOperationType.REMOVE] = EditOperationType.REMOVE
    source_start_us: int = Field(..., ge=0)
    source_end_us: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> RemoveInterval:
        if self.source_end_us <= self.source_start_us:
            raise ValueError("source_end_us 必须大于 source_start_us")
        return self


class ModifyInterval(EDLOperation):
    operation_type: Literal[EditOperationType.MODIFY] = EditOperationType.MODIFY
    old_source_start_us: int = Field(..., ge=0)
    new_source_start_us: int = Field(..., ge=0)
    new_source_end_us: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> ModifyInterval:
        if self.new_source_end_us <= self.new_source_start_us:
            raise ValueError("new_source_end_us 必须大于 new_source_start_us")
        return self


class MergeIntervals(EDLOperation):
    operation_type: Literal[EditOperationType.MERGE] = EditOperationType.MERGE
    first_source_start_us: int = Field(..., ge=0)
    second_source_end_us: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> MergeIntervals:
        if self.second_source_end_us <= self.first_source_start_us:
            raise ValueError("second_source_end_us 必须大于 first_source_start_us")
        return self


class SplitInterval(EDLOperation):
    operation_type: Literal[EditOperationType.SPLIT] = EditOperationType.SPLIT
    source_start_us: int = Field(..., ge=0)
    split_at_us: int = Field(..., ge=0)
