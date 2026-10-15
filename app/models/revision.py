"""ProjectRevision 模型 — 项目版本管理与 Undo/Redo 支持。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import OperationType

#: EDL 快照与操作详情的上限：快照是整份 EDL 的 JSON，详情只放少量元数据
_MAX_SNAPSHOT_CHARS = 4_000_000


class ProjectRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: UUID = Field(default_factory=uuid4, description="版本 ID")
    project_id: UUID = Field(..., description="项目 ID")
    parent_revision_id: UUID | None = Field(default=None, description="父版本 ID")
    edl_snapshot: str = Field(
        ..., min_length=2, max_length=_MAX_SNAPSHOT_CHARS, description="EDL JSON 快照"
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC), description="创建时间"
    )
    operation_type: OperationType = Field(..., description="操作类型")
    operation_detail: dict[str, Any] = Field(
        default_factory=dict, description="操作详情"
    )
    revision_number: int = Field(default=0, description="版本序号", ge=0)

    @field_validator("operation_detail")
    @classmethod
    def limit_detail(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 64:
            raise ValueError("operation_detail 键数量上限为 64")
        return v

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ProjectRevision:
        return cls.model_validate(data)
