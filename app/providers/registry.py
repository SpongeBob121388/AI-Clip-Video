"""Provider 装配 —— 按配置与凭据库构造并复用 provider 实例。

为什么必须单例：ASR 模型可达数 GB，按请求重建等于每次识别都重新加载权重；
FFmpegRunner 的 ffprobe 探测、VAD 模型加载同理。

诚实性约定：`available_*` 只报告"真的能跑"的引擎。WhisperX / Qwen ASR / 数字人
目前是未实现分支，它们必须从可用列表里消失，而不是 is_available() 返回 True 后
在 transcribe() 里抛 NotImplementedError —— 那样 UI 会显示成功然后什么也没发生。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.models.enums import ASREngine, DetectionMethod
from app.providers.asr.faster_whisper_adapter import FasterWhisperAdapter
from app.providers.avatar.did_adapter import DIDAdapter
from app.providers.avatar.heygen_adapter import HeyGenAdapter
from app.providers.credential.win_credential_store import get_credential_store
from app.providers.ffmpeg.runner import FFmpegRunner
from app.providers.llm.openai_adapter import OpenAILLMAdapter
from app.providers.vad.silero_vad_adapter import SileroVADAdapter
from app.providers.vad.volume_detector import VolumeDetector
from app.utils.logging import get_logger

logger = get_logger("providers.registry")


@dataclass
class Providers:
    settings: Settings
    ffmpeg: FFmpegRunner = field(default_factory=lambda: FFmpegRunner())

    def _memo(self) -> dict[str, object]:
        if not hasattr(self, "_cache"):
            self._cache = {}
        return self._cache  # type: ignore[attr-defined,no-any-return]

    def asr(self, engine: ASREngine) -> object:
        cache = self._memo()
        key = f"asr:{engine.value}"
        if key not in cache:
            if engine is ASREngine.FASTER_WHISPER:
                cache[key] = FasterWhisperAdapter(
                    model_cache_dir=str(self.settings.data_dir / "models"),
                    hf_endpoint=self.settings.asr_hf_endpoint,
                )
            else:
                raise ValueError(f"ASR 引擎未实现: {engine.value}")
        return cache[key]

    def vad(self, method: DetectionMethod) -> object:
        cache = self._memo()
        key = f"vad:{method.value}"
        if key not in cache:
            if method is DetectionMethod.VOLUME:
                cache[key] = VolumeDetector()
            else:
                cache[key] = SileroVADAdapter()
        return cache[key]

    def llm(self, model_name: str = "gpt-4o-mini") -> OpenAILLMAdapter:
        cache = self._memo()
        key = f"llm:{model_name}"
        if key not in cache:
            base_url = get_credential_store().get_secret("llm_base_url")
            cache[key] = OpenAILLMAdapter(
                credential_store=get_credential_store(),
                model_name=model_name,
                base_url=base_url,
            )
        return cache[key]  # type: ignore[return-value]

    def preferred_vad(self) -> object:
        """Silero 可用就用它，否则音量检测；两条路径都是真实现。"""
        vad = self.vad(DetectionMethod.VAD)
        if hasattr(vad, "is_available") and vad.is_available():  # type: ignore[union-attr]
            return vad
        return self.vad(DetectionMethod.VOLUME)

    def available_asr_engines(self) -> list[ASREngine]:
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is None:
            return []
        return [ASREngine.FASTER_WHISPER]

    def avatar(self, provider: str) -> HeyGenAdapter | DIDAdapter:
        """数字人适配器。当前为未实现分支，调用方须先检查 is_available()。"""
        cache = self._memo()
        key = f"avatar:{provider}"
        if key not in cache:
            if provider == "heygen":
                cache[key] = HeyGenAdapter(get_credential_store())
            elif provider == "did":
                cache[key] = DIDAdapter(get_credential_store())
            else:
                raise ValueError(f"未知数字人服务: {provider}")
        return cache[key]  # type: ignore[return-value]

    async def close(self) -> None:
        for value in getattr(self, "_cache", {}).values():
            aclose = getattr(value, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception as exc:  # noqa: BLE001 - 关闭失败不影响其余资源
                    logger.warning("provider_close_failed", error=str(exc))
        self._cache = {}


_providers: Providers | None = None


async def shutdown_providers() -> None:
    global _providers
    if _providers is not None:
        await _providers.close()
    _providers = None


def get_providers() -> Providers:
    global _providers
    if _providers is None:
        _providers = Providers(settings=get_settings())
        logger.info("providers_initialized", data_dir=str(_providers.settings.data_dir))
    return _providers


def reset_providers() -> None:
    global _providers
    _providers = None
