"""faster-whisper ASR Adapter。

使用 faster_whisper.WhisperModel（CTranslate2 后端），支持 CPU 模式。
输出 utterance 级和 word 级时间戳，转换为微秒。
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.models.asr_result import ASRResult, Utterance, WordSpan
from app.models.enums import ASREngine, ModelSize
from app.utils.logging import get_logger

logger = get_logger("providers.asr.faster_whisper")


class FasterWhisperAdapter:
    def __init__(
        self,
        model_cache_dir: str | None = None,
        device: str = "cpu",
        hf_endpoint: str | None = None,
    ) -> None:
        self._model_cache_dir = model_cache_dir
        self._device = device
        self._hf_endpoint = hf_endpoint
        self._models: dict[ModelSize, object] = {}
        self._load_lock = asyncio.Lock()

    @property
    def engine_name(self) -> ASREngine:
        return ASREngine.FASTER_WHISPER

    def is_available(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("faster_whisper") is not None
        except (ImportError, ValueError):
            return False

    async def download_model(
        self,
        model_size: ModelSize,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> None:
        if progress_callback:
            progress_callback(0.0, f"下载 {model_size.value} 模型")
        try:
            # 权重下载可能数百 MB 且是阻塞 IO
            await asyncio.to_thread(self._load_model_into_cache, model_size)
        except ImportError:
            logger.error("faster_whisper_not_installed")
            raise
        if progress_callback:
            progress_callback(1.0, "模型下载完成")
        logger.info("model_downloaded", model_size=model_size.value)

    def _apply_hf_endpoint(self) -> None:
        """模型下载源。huggingface_hub 在导入期读 HF_ENDPOINT，
        所以既要在懒导入之前设置，也要覆盖已经导入完成的情况。"""
        endpoint = (self._hf_endpoint or "").strip().rstrip("/")
        if not endpoint:
            return
        if not endpoint.startswith(("http://", "https://")):
            logger.warning("asr_hf_endpoint_ignored", reason="不是 http(s) 地址")
            return
        os.environ["HF_ENDPOINT"] = endpoint
        constants = sys.modules.get("huggingface_hub.constants")
        if constants is not None:
            constants.HF_ENDPOINT = endpoint

    def _load_model_into_cache(self, model_size: ModelSize) -> object:
        # Anaconda 的 MKL 与 ctranslate2 各自带一份 libiomp5md，两份同时初始化时
        # Intel 的运行时不会抛异常而是直接 abort 整个进程（OMP Error #15）。
        # 必须在加载模型之前设置：OpenMP 只在首次初始化时读它。
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        self._apply_hf_endpoint()
        from faster_whisper import WhisperModel

        try:
            model = WhisperModel(
                model_size.value,
                device=self._device,
                download_root=self._model_cache_dir,
            )
        except Exception as exc:
            raise RuntimeError(self._describe_load_failure(exc)) from exc
        self._models[model_size] = model
        return model

    def _describe_load_failure(self, exc: Exception) -> str:
        """把下载/加载异常翻成一句可操作的中文原因。"""
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        if "timeout" in lowered or "connection" in lowered or "unreachable" in lowered:
            return (
                f"模型下载失败，无法访问模型源（当前 HF_ENDPOINT="
                f"{os.environ.get('HF_ENDPOINT', 'https://huggingface.co')}）。"
                "国内网络可设置环境变量 ACV_ASR_HF_ENDPOINT=https://hf-mirror.com 后重试"
            )
        if "compute type" in lowered or "access violation" in lowered or "segmentation" in lowered:
            return f"推理库加载模型失败：{text[:180]}"
        return text[:200]

    async def _get_model(self, model_size: ModelSize) -> object:
        """加锁加载：并发请求同时落到冷缓存时只加载一次（模型可达数 GB）。"""
        cached = self._models.get(model_size)
        if cached is not None:
            return cached
        async with self._load_lock:
            if model_size not in self._models:
                return await asyncio.to_thread(self._load_model_into_cache, model_size)
            return self._models[model_size]

    async def transcribe(
        self,
        audio_path: str,
        language: str = "zh",
        model_size: ModelSize = ModelSize.MEDIUM,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")

        if progress_callback:
            progress_callback(0.0, "加载模型")

        model = await self._get_model(model_size)

        if progress_callback:
            progress_callback(0.1, "开始识别")

        # CTranslate2 推理是长时间阻塞调用，必须离开事件循环
        utterances, total_duration_us = await asyncio.to_thread(
            self._transcribe_sync, model, audio_path, language
        )

        if progress_callback:
            progress_callback(1.0, "识别完成")

        logger.info(
            "transcribe_completed",
            utterances=len(utterances),
            duration_us=total_duration_us,
            language=language,
        )

        return ASRResult(
            engine=ASREngine.FASTER_WHISPER,
            language=language,
            model_size=model_size,
            utterances=utterances,
            total_duration_us=total_duration_us,
        )

    @staticmethod
    def _transcribe_sync(
        model: object, audio_path: str, language: str
    ) -> tuple[list[Utterance], int]:
        segments, _info = model.transcribe(
            audio_path,
            language=language if language != "auto" else None,
            word_timestamps=True,
            # 不开 Whisper 自带 VAD：它依赖 onnxruntime，部分环境（Anaconda + MKL）导入即崩溃，
            # 而且本项目的静音检测已经基于同一条波形，重复过滤没有收益
            vad_filter=False,
        )

        utterances: list[Utterance] = []
        total_duration_us = 0

        for segment in segments:
            words: list[WordSpan] = []
            for word in segment.words or []:
                start_us = int(word.start * 1_000_000)
                end_us = int(word.end * 1_000_000)
                if end_us <= start_us:
                    continue
                words.append(
                    WordSpan(
                        word_id=f"w-{uuid4().hex[:8]}",
                        start_us=start_us,
                        end_us=end_us,
                        text=word.word.strip()[:500],
                        confidence=float(word.probability),
                    )
                )

            start_us = int(segment.start * 1_000_000)
            end_us = int(segment.end * 1_000_000)
            if end_us <= start_us:
                continue
            utterances.append(
                Utterance(
                    utterance_id=f"u-{uuid4().hex[:8]}",
                    start_us=start_us,
                    end_us=end_us,
                    text=segment.text.strip()[:5000],
                    words=words,
                )
            )
            total_duration_us = max(total_duration_us, end_us)

        return utterances, total_duration_us
