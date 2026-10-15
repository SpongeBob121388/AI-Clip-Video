"""静音检测 Service — 编排 VAD/音量检测、候选去重合并、ASR 交叉对齐。"""

from __future__ import annotations

import bisect
import math
from collections.abc import Callable

from app.models.asr_result import ASRResult
from app.models.enums import DetectionMethod
from app.models.silence_candidate import SilenceCandidate
from app.providers.vad.protocol import VADParams, VADProviderProtocol
from app.utils.logging import get_logger

logger = get_logger("services.detect_service")

_MERGE_GAP_US = 300_000


class SilenceDetectServiceError(RuntimeError):
    pass


def _weighted_dbfs(first: SilenceCandidate, second: SilenceCandidate, merged_end_us: int) -> float:
    """按段长加权的 dBFS 合并。

    dBFS 是对数单位，直接平均是错的；先回到功率域加权再取对数。
    """
    d1 = max(0, min(first.end_us, merged_end_us) - first.start_us)
    d2 = max(0, min(second.end_us, merged_end_us) - second.start_us)
    if d1 + d2 == 0:
        return first.avg_dbfs

    power = (
        (10 ** (first.avg_dbfs / 10) * d1) + (10 ** (second.avg_dbfs / 10) * d2)
    ) / (d1 + d2)
    if power <= 0:
        return -120.0
    return max(-120.0, min(0.0, 10 * math.log10(power)))


class SilenceDetectService:
    def __init__(self, vad_provider: VADProviderProtocol) -> None:
        self.vad_provider = vad_provider

    async def detect(
        self,
        audio_path: str,
        params: VADParams,
        asr_result: ASRResult | None = None,
        progress_callback: Callable[[float, str], None] | None = None,
        total_duration_us: int | None = None,
    ) -> list[SilenceCandidate]:
        candidates = await self.vad_provider.detect_silence(
            audio_path, params, progress_callback
        )

        candidates = self.merge_candidates(candidates, gap_us=params.merge_gap_us)

        if asr_result:
            candidates = self.align_with_asr(candidates, asr_result)

        # 以媒体真实时长为分母；旧实现用"最后一个候选的结束点"，
        # 于是长视频里唯一一段静音也会被判成"整个视频均为静音"
        self._check_all_silence(candidates, total_duration_us)

        logger.info("detect_completed", candidates=len(candidates))
        return candidates

    @staticmethod
    def merge_candidates(
        candidates: list[SilenceCandidate], gap_us: int = _MERGE_GAP_US
    ) -> list[SilenceCandidate]:
        """合并间隔足够近的静音段。

        返回新对象（不改写 provider 交给我们的实例），并按段长加权重算 avg_dbfs，
        否则合并后的片段会沿用第一段的音量，报告值失真。
        """
        if not candidates:
            return []

        sorted_cands = sorted(candidates, key=lambda c: c.start_us)
        merged: list[SilenceCandidate] = [sorted_cands[0].model_copy(deep=True)]

        for cand in sorted_cands[1:]:
            last = merged[-1]
            gap = cand.start_us - last.end_us

            if gap <= gap_us:
                new_end = max(last.end_us, cand.end_us)
                last.avg_dbfs = _weighted_dbfs(last, cand, new_end)
                last.end_us = new_end
                if not last.prev_utterance_text:
                    last.prev_utterance_text = cand.prev_utterance_text
                if not last.next_utterance_text:
                    last.next_utterance_text = cand.next_utterance_text
                last.confidence = max(last.confidence, cand.confidence)
            else:
                merged.append(cand.model_copy(deep=True))

        return merged

    @staticmethod
    def align_with_asr(
        candidates: list[SilenceCandidate], asr_result: ASRResult
    ) -> list[SilenceCandidate]:
        """为每个静音候选填充前后语句文本。"""
        if not candidates:
            return candidates

        utterances = sorted(asr_result.utterances, key=lambda u: u.start_us)
        starts = [u.start_us for u in utterances]
        result: list[SilenceCandidate] = []

        for cand in candidates:
            item = cand.model_copy(deep=True)
            # 候选起点之前的最后一个语句（含跨过候选起点的语句）作为前文
            index = bisect.bisect_right(starts, cand.start_us)
            prev_text = ""
            for utt in utterances[:index]:
                if utt.end_us <= cand.end_us:
                    prev_text = utt.text
            next_index = bisect.bisect_left(starts, cand.end_us)
            next_text = utterances[next_index].text if next_index < len(utterances) else ""

            item.prev_utterance_text = prev_text
            item.next_utterance_text = next_text
            result.append(item)

        return result

    @staticmethod
    def _check_all_silence(
        candidates: list[SilenceCandidate],
        total_duration_us: int | None = None,
        ratio: float = 0.95,
    ) -> None:
        if not candidates or not total_duration_us or total_duration_us <= 0:
            return

        total_silence = sum(c.end_us - c.start_us for c in candidates)
        if total_silence >= total_duration_us * ratio:
            raise SilenceDetectServiceError("未检测到有效语音内容，整个视频均为静音")

    @staticmethod
    def combine_candidates(
        vad_candidates: list[SilenceCandidate],
        volume_candidates: list[SilenceCandidate],
    ) -> list[SilenceCandidate]:
        all_cands = vad_candidates + volume_candidates
        merged = SilenceDetectService.merge_candidates(all_cands)
        for c in merged:
            c.detection_method = DetectionMethod.COMBINED
        return merged
