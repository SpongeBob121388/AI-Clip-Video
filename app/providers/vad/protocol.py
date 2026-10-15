"""VAD Provider Protocol 接口定义。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.models.silence_candidate import SilenceCandidate


class VADParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    threshold: float = Field(default=0.5, description="VAD 阈值 [0.0, 1.0]", ge=0.0, le=1.0)
    min_silence_duration_us: int = Field(
        default=500_000, description="最小静音时长（微秒）", ge=100_000
    )
    min_keep_duration_us: int = Field(
        default=1_000_000, description="最小保留语音时长（微秒）", ge=100_000
    )
    dbfs_threshold: float = Field(
        default=-40.0, description="音量阈值（dBFS）", le=0.0
    )
    merge_gap_us: int = Field(
        default=300_000,
        ge=0,
        description="间隔小于该值的静音候选合并为一段（微秒）",
    )


@runtime_checkable
class VADProviderProtocol(Protocol):
    async def detect_silence(
        self,
        audio_path: str,
        params: VADParams,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> list[SilenceCandidate]: ...

    def is_available(self) -> bool: ...
