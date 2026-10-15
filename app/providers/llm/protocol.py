"""LLM Provider Protocol 接口定义。

定义 LLM 剪辑决策的领域契约接口，支持 OpenAI 兼容接口的 Adapter 实现。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import Decision, DecisionSource, SuggestionType


class CandidateContext(BaseModel):
    id: str = Field(..., description="候选 ID")
    prev_text: str = Field(default="", description="前一 utterance 文本")
    next_text: str = Field(default="", description="后一 utterance 文本")
    duration_us: int = Field(..., description="候选时长（微秒）", ge=0)
    type: SuggestionType = Field(..., description="候选类型")


class CandidateDecision(BaseModel):
    id: str = Field(..., description="候选 ID")
    decision: Decision = Field(..., description="决策结果")
    reason: str = Field(default="", description="决策理由")
    decision_source: DecisionSource = Field(..., description="决策来源")
    timestamp: str = Field(..., description="决策时间戳（ISO 8601 UTC）")


@runtime_checkable
class LLMProviderProtocol(Protocol):
    async def suggest_decisions(
        self, candidates: list[CandidateContext]
    ) -> list[CandidateDecision]: ...

    def is_available(self) -> bool: ...
