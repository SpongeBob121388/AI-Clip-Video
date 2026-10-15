"""Silero VAD Adapter — 使用 Silero VAD 模型检测静音片段。

约定：只有"模型不可用（依赖缺失/加载失败）"才降级到音量检测；
推理过程中的异常一律向上抛，避免把真实故障伪装成"未检测到静音"。
推理与解码都是同步 CPU 工作，放线程池执行以免冻结事件循环。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.models.enums import DetectionMethod
from app.models.silence_candidate import SilenceCandidate
from app.providers.vad.protocol import VADParams
from app.utils.logging import get_logger

logger = get_logger("providers.vad.silero_vad")

_WINDOW_SIZE = 512
_TARGET_SAMPLE_RATE = 16_000


class SileroVADAdapter:
    def __init__(self) -> None:
        self._model: object | None = None

    def is_available(self) -> bool:
        try:
            import importlib.util

            return (
                importlib.util.find_spec("silero_vad") is not None
                and importlib.util.find_spec("torch") is not None
            )
        except (ImportError, ValueError):
            return False

    def _load_model(self) -> object:
        if self._model is not None:
            return self._model
        from silero_vad import load_silero_vad

        self._model = load_silero_vad()
        logger.info("silero_vad_model_loaded")
        return self._model

    async def detect_silence(
        self,
        audio_path: str,
        params: VADParams,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> list[SilenceCandidate]:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")

        try:
            model = await asyncio.to_thread(self._load_model)
        except Exception as exc:
            logger.warning("vad_model_unavailable_fallback_to_volume", error=str(exc))
            from app.providers.vad.volume_detector import VolumeDetector

            return await VolumeDetector().detect_silence(audio_path, params, progress_callback)

        if progress_callback:
            progress_callback(0.0, "Silero VAD 检测中")

        chunks, total_duration_us = await asyncio.to_thread(
            self._run_vad, model, audio_path, params.threshold
        )

        candidates: list[SilenceCandidate] = []
        for start_us, end_us in chunks:
            if end_us - start_us < params.min_silence_duration_us:
                continue
            candidates.append(
                SilenceCandidate(
                    id=f"sil-{uuid4().hex[:8]}",
                    start_us=start_us,
                    end_us=min(end_us, total_duration_us),
                    detection_method=DetectionMethod.VAD,
                    avg_dbfs=-60.0,
                )
            )

        if progress_callback:
            progress_callback(1.0, f"检测到 {len(candidates)} 个静音片段")

        logger.info("vad_detection_completed", candidates=len(candidates))
        return candidates

    def _run_vad(self, model: object, audio_path: str, threshold: float) -> tuple[list[tuple[int, int]], int]:
        import torch
        import torchaudio

        waveform, sample_rate = torchaudio.load(audio_path)
        waveform = waveform[0]
        if sample_rate != _TARGET_SAMPLE_RATE:
            resampler = torchaudio.transforms.Resample(sample_rate, _TARGET_SAMPLE_RATE)
            waveform = resampler(waveform)
        sample_rate = _TARGET_SAMPLE_RATE

        total = len(waveform)
        total_duration_us = int(total / sample_rate * 1_000_000)

        silence_chunks: list[tuple[int, int]] = []
        silence_start: int | None = None

        with torch.no_grad():
            for i in range(0, total, _WINDOW_SIZE):
                chunk = waveform[i : i + _WINDOW_SIZE].unsqueeze(0)
                if chunk.shape[1] < _WINDOW_SIZE:
                    # 末尾不足一窗的片段不足以判定，直接跳过而不是误判为静音
                    break
                speech_prob = float(model(chunk, sample_rate).item())
                time_us = int(i / sample_rate * 1_000_000)

                if speech_prob < threshold:
                    if silence_start is None:
                        silence_start = time_us
                elif silence_start is not None:
                    silence_chunks.append((silence_start, time_us))
                    silence_start = None

        if silence_start is not None:
            silence_chunks.append((silence_start, total_duration_us))

        return silence_chunks, total_duration_us
