"""ASR Provider Protocol 接口定义。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from app.models.asr_result import ASRResult
from app.models.enums import ASREngine, ModelSize


@runtime_checkable
class ASRProviderProtocol(Protocol):
    async def transcribe(
        self,
        audio_path: str,
        language: str = "zh",
        model_size: ModelSize = ModelSize.MEDIUM,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult: ...

    def is_available(self) -> bool: ...

    async def download_model(
        self,
        model_size: ModelSize,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> None: ...

    @property
    def engine_name(self) -> ASREngine: ...
