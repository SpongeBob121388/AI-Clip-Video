"""VAD 静音检测与候选生成单元测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.asr_result import ASRResult, Utterance
from app.models.enums import ASREngine, DetectionMethod, ModelSize
from app.models.silence_candidate import SilenceCandidate
from app.providers.vad.protocol import VADParams, VADProviderProtocol
from app.providers.vad.silero_vad_adapter import SileroVADAdapter
from app.providers.vad.volume_detector import VolumeDetector
from app.services.detect_service import SilenceDetectService, SilenceDetectServiceError


def _make_candidate(
    cid: str,
    start: int,
    end: int,
    method: DetectionMethod = DetectionMethod.VAD,
    avg_dbfs: float = -50.0,
) -> SilenceCandidate:
    return SilenceCandidate(
        id=cid,
        start_us=start,
        end_us=end,
        detection_method=method,
        avg_dbfs=avg_dbfs,
    )


@pytest.mark.unit
class TestVADProtocol:
    def test_silero_protocol(self) -> None:
        adapter = SileroVADAdapter()
        assert isinstance(adapter, VADProviderProtocol)

    def test_volume_protocol(self) -> None:
        detector = VolumeDetector()
        assert isinstance(detector, VADProviderProtocol)

    def test_silero_is_available(self) -> None:
        adapter = SileroVADAdapter()
        assert isinstance(adapter.is_available(), bool)

    def test_volume_is_available(self) -> None:
        detector = VolumeDetector()
        assert detector.is_available() is True


@pytest.mark.unit
class TestVADParams:
    def test_default_params(self) -> None:
        params = VADParams()
        assert params.threshold == 0.5
        assert params.min_silence_duration_us == 500_000
        assert params.dbfs_threshold == -40.0

    def test_custom_params(self) -> None:
        params = VADParams(
            threshold=0.3,
            min_silence_duration_us=300_000,
            dbfs_threshold=-50.0,
        )
        assert params.threshold == 0.3
        assert params.dbfs_threshold == -50.0


@pytest.mark.unit
class TestCandidateMerge:
    def test_no_merge_needed(self) -> None:
        candidates = [
            _make_candidate("s1", 0, 500_000),
            _make_candidate("s2", 1_000_000, 1_500_000),
        ]
        merged = SilenceDetectService.merge_candidates(candidates)
        assert len(merged) == 2

    def test_merge_close_candidates(self) -> None:
        candidates = [
            _make_candidate("s1", 0, 500_000),
            _make_candidate("s2", 600_000, 1_000_000),
        ]
        merged = SilenceDetectService.merge_candidates(candidates)
        assert len(merged) == 1
        assert merged[0].start_us == 0
        assert merged[0].end_us == 1_000_000

    def test_merge_gap_boundary(self) -> None:
        candidates = [
            _make_candidate("s1", 0, 500_000),
            _make_candidate("s2", 800_000, 1_000_000),
        ]
        merged = SilenceDetectService.merge_candidates(candidates)
        assert len(merged) == 1

    def test_empty_candidates(self) -> None:
        merged = SilenceDetectService.merge_candidates([])
        assert merged == []

    def test_unsorted_input(self) -> None:
        candidates = [
            _make_candidate("s2", 1_000_000, 1_500_000),
            _make_candidate("s1", 0, 500_000),
        ]
        merged = SilenceDetectService.merge_candidates(candidates)
        assert merged[0].start_us == 0


@pytest.mark.unit
class TestASRAlignment:
    def test_align_with_asr(self) -> None:
        candidates = [_make_candidate("s1", 3_000_000, 5_000_000)]
        asr_result = ASRResult(
            engine=ASREngine.FASTER_WHISPER,
            language="zh",
            model_size=ModelSize.MEDIUM,
            utterances=[
                Utterance(
                    utterance_id="u1",
                    start_us=0,
                    end_us=2_000_000,
                    text="大家好",
                ),
                Utterance(
                    utterance_id="u2",
                    start_us=6_000_000,
                    end_us=8_000_000,
                    text="欢迎观看",
                ),
            ],
        )
        aligned = SilenceDetectService.align_with_asr(candidates, asr_result)
        assert aligned[0].prev_utterance_text == "大家好"
        assert aligned[0].next_utterance_text == "欢迎观看"

    def test_align_no_asr(self) -> None:
        candidates = [_make_candidate("s1", 0, 500_000)]
        aligned = SilenceDetectService.align_with_asr(candidates, MagicMock(utterances=[]))
        assert aligned[0].prev_utterance_text == ""


@pytest.mark.unit
class TestSilenceDetectService:
    async def test_detect_flow(self) -> None:
        mock_provider = MagicMock(spec=VADProviderProtocol)
        mock_provider.detect_silence = AsyncMock(
            return_value=[
                _make_candidate("s1", 1_000_000, 2_000_000),
                _make_candidate("s2", 2_100_000, 3_000_000),
            ]
        )
        service = SilenceDetectService(mock_provider)
        result = await service.detect("test.wav", VADParams())
        assert len(result) == 1
        assert result[0].start_us == 1_000_000
        assert result[0].end_us == 3_000_000

    async def test_all_silence_rejected(self) -> None:
        mock_provider = MagicMock(spec=VADProviderProtocol)
        mock_provider.detect_silence = AsyncMock(
            return_value=[_make_candidate("s1", 0, 10_000_000)]
        )
        service = SilenceDetectService(mock_provider)
        with pytest.raises(SilenceDetectServiceError, match="未检测到有效语音"):
            await service.detect("test.wav", VADParams(), total_duration_us=10_000_000)

    async def test_single_silence_in_long_video_not_flagged(self) -> None:
        """回归：旧实现拿"最后一个候选的结束点"当视频总时长，
        于是 100s 视频里唯一一段 3s 静音也会报"整个视频均为静音"。"""
        mock_provider = MagicMock(spec=VADProviderProtocol)
        mock_provider.detect_silence = AsyncMock(
            return_value=[_make_candidate("s1", 10_000_000, 13_000_000)]
        )
        service = SilenceDetectService(mock_provider)
        result = await service.detect(
            "test.wav", VADParams(), total_duration_us=100_000_000
        )
        assert len(result) == 1

    async def test_merge_does_not_mutate_provider_objects(self) -> None:
        source = [
            _make_candidate("s1", 0, 500_000),
            _make_candidate("s2", 600_000, 1_000_000),
        ]
        original_end = source[0].end_us
        SilenceDetectService.merge_candidates(source)
        assert source[0].end_us == original_end, "provider 交给我们的对象不应被改写"

    async def test_merge_reweights_dbfs(self) -> None:
        """dBFS 是对数单位，合并必须回到功率域加权，也不能沿用第一段的值。"""
        equal = SilenceDetectService.merge_candidates(
            [
                _make_candidate("s1", 0, 2_000_000, avg_dbfs=-50.0),
                _make_candidate("s2", 2_100_000, 4_000_000, avg_dbfs=-50.0),
            ]
        )
        assert len(equal) == 1
        assert equal[0].avg_dbfs == pytest.approx(-50.0, abs=0.5)

        mixed = SilenceDetectService.merge_candidates(
            [
                _make_candidate("s1", 0, 4_000_000, avg_dbfs=-90.0),
                _make_candidate("s2", 4_100_000, 5_000_000, avg_dbfs=-20.0),
            ]
        )
        assert len(mixed) == 1
        # 结果落在两段之间，且不等于"第一段的 -90"（旧实现的失真值）
        assert -90.0 < mixed[0].avg_dbfs < -20.0

    def test_combine_candidates(self) -> None:
        vad_cands = [_make_candidate("v1", 0, 500_000, DetectionMethod.VAD)]
        vol_cands = [_make_candidate("v2", 600_000, 1_000_000, DetectionMethod.VOLUME)]
        combined = SilenceDetectService.combine_candidates(vad_cands, vol_cands)
        assert len(combined) == 1
        assert combined[0].detection_method == DetectionMethod.COMBINED
