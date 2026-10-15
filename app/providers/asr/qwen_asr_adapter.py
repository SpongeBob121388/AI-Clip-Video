"""Qwen ASR Adapter — Qwen3-ASR 引擎。"""

from __future__ import annotations

from collections.abc import Callable

from app.models.asr_result import ASRResult
from app.models.enums import ASREngine, ModelSize
from app.utils.logging import get_logger

logger = get_logger("providers.asr.qwen_asr")


class QwenASRAdapter:
    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url

    @property
    def engine_name(self) -> ASREngine:
        return ASREngine.QWEN_ASR

    def is_available(self) -> bool:
        return self._api_key is not None

    async def download_model(
        self,
        model_size: ModelSize,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> None:
        if progress_callback:
            progress_callback(1.0, "Qwen ASR 云端模型无需下载")

    async def transcribe(
        self,
        audio_path: str,
        language: str = "zh",
        model_size: ModelSize = ModelSize.MEDIUM,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        raise NotImplementedError("Qwen ASR Adapter 将在 POC 验证后实现")
