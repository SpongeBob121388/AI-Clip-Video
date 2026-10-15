"""WhisperX ASR Adapter — 词级对齐精修。"""

from __future__ import annotations

from collections.abc import Callable

from app.models.asr_result import ASRResult
from app.models.enums import ASREngine, ModelSize
from app.utils.logging import get_logger

logger = get_logger("providers.asr.whisperx")


class WhisperXAdapter:
    def __init__(self, device: str = "cpu") -> None:
        self._device = device

    @property
    def engine_name(self) -> ASREngine:
        return ASREngine.WHISPERX

    def is_available(self) -> bool:
        try:
            import importlib.util
            return importlib.util.find_spec("whisperx") is not None
        except ImportError:
            return False

    async def download_model(
        self,
        model_size: ModelSize,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> None:
        if progress_callback:
            progress_callback(1.0, "WhisperX 模型下载完成")

    async def transcribe(
        self,
        audio_path: str,
        language: str = "zh",
        model_size: ModelSize = ModelSize.MEDIUM,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        raise NotImplementedError("WhisperX Adapter 将在 POC 验证后实现")
