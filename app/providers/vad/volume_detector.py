"""音量阈值检测器 — 基于 dBFS 阈值检测静音。

解码与逐窗扫描是 CPU 密集的同步工作，统一放到线程里执行，
避免长时间冻结事件循环（WebSocket 进度与 UI 都跑在这个循环上）。
"""

from __future__ import annotations

import array
import asyncio
import math
import sys
import wave
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from app.models.enums import DetectionMethod
from app.models.silence_candidate import SilenceCandidate
from app.providers.vad.protocol import VADParams
from app.utils.logging import get_logger

logger = get_logger("providers.vad.volume_detector")

_WINDOW_SIZE_MS = 100
_SILENT_DBFS = -100.0


class AudioDecodeError(RuntimeError):
    """音频无法解码。绝不返回空样本 —— 否则'检测成功但 0 段'会掩盖真实故障。"""


class VolumeDetector:
    def is_available(self) -> bool:
        return True

    async def detect_silence(
        self,
        audio_path: str,
        params: VADParams,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> list[SilenceCandidate]:
        if not Path(audio_path).exists():
            raise FileNotFoundError(f"音频文件不存在: {audio_path}")

        if progress_callback:
            progress_callback(0.0, "音量阈值检测中")

        chunks, total_duration_us = await asyncio.to_thread(
            self._analyze, audio_path, params
        )

        candidates: list[SilenceCandidate] = []
        for start_us, end_us, avg_dbfs in chunks:
            if end_us - start_us < params.min_silence_duration_us:
                continue
            candidates.append(
                SilenceCandidate(
                    id=f"sil-{uuid4().hex[:8]}",
                    start_us=start_us,
                    end_us=min(end_us, total_duration_us),
                    detection_method=DetectionMethod.VOLUME,
                    avg_dbfs=avg_dbfs,
                )
            )

        if progress_callback:
            progress_callback(1.0, f"检测到 {len(candidates)} 个静音片段")

        logger.info(
            "volume_detection_completed",
            candidates=len(candidates),
            duration_us=total_duration_us,
        )
        return candidates

    def _analyze(self, audio_path: str, params: VADParams) -> tuple[list[tuple[int, int, float]], int]:
        samples, sample_rate = self._read_audio(audio_path)
        if not samples:
            raise AudioDecodeError(f"音频不含可分析样本: {audio_path}")

        total_samples = len(samples)
        total_duration_us = int(total_samples / sample_rate * 1_000_000)
        window_samples = max(1, int(sample_rate * _WINDOW_SIZE_MS / 1000))

        chunks: list[tuple[int, int, float]] = []
        silence_start: int | None = None
        window_dbfs: list[float] = []

        for i in range(0, total_samples, window_samples):
            window = samples[i : i + window_samples]
            if not window:
                break

            dbfs = self._compute_dbfs(window)
            time_us = int(i / sample_rate * 1_000_000)

            if dbfs < params.dbfs_threshold:
                if silence_start is None:
                    silence_start = time_us
                window_dbfs.append(dbfs)
            elif silence_start is not None:
                avg = sum(window_dbfs) / len(window_dbfs) if window_dbfs else _SILENT_DBFS
                chunks.append((silence_start, time_us, avg))
                silence_start = None
                window_dbfs = []

        if silence_start is not None:
            avg = sum(window_dbfs) / len(window_dbfs) if window_dbfs else _SILENT_DBFS
            chunks.append((silence_start, total_duration_us, avg))

        return chunks, total_duration_us

    @staticmethod
    def _compute_dbfs(samples: array.array | list[float]) -> float:
        if not samples:
            return _SILENT_DBFS
        mean_sq = sum(s * s for s in samples) / len(samples)
        if mean_sq <= 0:
            return _SILENT_DBFS
        return 20 * math.log10(math.sqrt(mean_sq))

    def _read_audio(self, audio_path: str) -> tuple[array.array, int]:
        """优先 torchaudio，回退 stdlib wave。失败必须抛错，不可静默返回空。"""
        try:
            import torchaudio

            waveform, sample_rate = torchaudio.load(audio_path)
            data = waveform[0]
            return (data.tolist() if hasattr(data, "tolist") else list(data), int(sample_rate))
        except ImportError:
            pass
        except Exception as exc:  # torchaudio 存在但解码失败（编码不支持等）
            raise AudioDecodeError(f"torchaudio 解码失败: {exc}") from exc

        return self._read_wav(audio_path)

    @staticmethod
    def _read_wav(audio_path: str) -> tuple[array.array, int]:
        try:
            with wave.open(audio_path, "rb") as wf:
                sample_rate = wf.getframerate()
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                raw = wf.readframes(wf.getnframes())
        except (wave.Error, OSError) as exc:
            raise AudioDecodeError(f"不是可解析的 WAV: {audio_path}（{exc}）") from exc

        if not sample_rate or sample_rate <= 0:
            raise AudioDecodeError(f"非法采样率: {sample_rate}")

        if sampwidth == 2:
            # array 批量解码，比逐样本 struct.unpack 快一至两个数量级
            samples = array.array("h")
            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
            if sys.byteorder == "big":  # pragma: no cover - Windows 恒为小端
                samples.byteswap()
            scaled = array.array("d", (s / 32768.0 for s in samples[::max(1, n_channels)]))
            return scaled, sample_rate

        if sampwidth == 1:
            unsigned = array.array("B", raw)
            return array.array("d", ((u - 128) / 128.0 for u in unsigned[::max(1, n_channels)])), sample_rate

        raise AudioDecodeError(
            f"不支持的位深 {sampwidth * 8}-bit；请先用 ffmpeg 转成 16-bit PCM WAV"
        )
