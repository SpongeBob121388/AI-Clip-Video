"""ASR Provider 单元测试。"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.asr_result import ASRResult
from app.models.enums import ASREngine, ModelSize
from app.models.media import MediaProbe, MediaTrack
from app.providers.asr.faster_whisper_adapter import FasterWhisperAdapter
from app.providers.asr.protocol import ASRProviderProtocol
from app.providers.asr.qwen_asr_adapter import QwenASRAdapter
from app.providers.asr.whisperx_adapter import WhisperXAdapter
from app.providers.ffmpeg.runner import FFmpegRunner
from app.services.asr_service import ASRService, ASRServiceError


@pytest.mark.unit
class TestASRProtocol:
    def test_faster_whisper_protocol(self) -> None:
        adapter = FasterWhisperAdapter()
        assert isinstance(adapter, ASRProviderProtocol)

    def test_whisperx_protocol(self) -> None:
        adapter = WhisperXAdapter()
        assert isinstance(adapter, ASRProviderProtocol)

    def test_qwen_protocol(self) -> None:
        adapter = QwenASRAdapter(api_key="test")
        assert isinstance(adapter, ASRProviderProtocol)


@pytest.mark.unit
class TestFasterWhisperAdapter:
    def test_engine_name(self) -> None:
        adapter = FasterWhisperAdapter()
        assert adapter.engine_name == ASREngine.FASTER_WHISPER

    def test_is_available(self) -> None:
        adapter = FasterWhisperAdapter()
        assert isinstance(adapter.is_available(), bool)

    async def test_transcribe_file_not_found(self) -> None:
        adapter = FasterWhisperAdapter()
        with pytest.raises(FileNotFoundError):
            await adapter.transcribe("/nonexistent/audio.wav")

    def test_transcribe_does_not_use_whisper_vad_filter(self) -> None:
        """onnxruntime 在部分环境导入即崩进程，Python 侧捕获不到，所以不能依赖它。"""
        recorded: dict[str, object] = {}

        class _FakeModel:
            def transcribe(self, _audio, **kwargs):
                recorded.update(kwargs)
                segment = SimpleNamespace(start=0.0, end=1.0, text="你好", words=[])
                return iter([segment]), SimpleNamespace(language="zh")

        utterances, duration = FasterWhisperAdapter._transcribe_sync(
            _FakeModel(), "ignored.wav", "zh"
        )
        assert recorded["vad_filter"] is False
        assert recorded["word_timestamps"] is True
        assert duration == 1_000_000
        assert utterances[0].text == "你好"

    def test_hf_endpoint_is_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        adapter = FasterWhisperAdapter(hf_endpoint="https://hf-mirror.com/")
        adapter._apply_hf_endpoint()
        assert os.environ["HF_ENDPOINT"] == "https://hf-mirror.com"

    def test_hf_endpoint_rejects_non_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        adapter = FasterWhisperAdapter(hf_endpoint="file:///etc/passwd")
        adapter._apply_hf_endpoint()
        assert "HF_ENDPOINT" not in os.environ

    def test_download_failure_message_points_at_mirror(self) -> None:
        adapter = FasterWhisperAdapter()
        message = adapter._describe_load_failure(
            ConnectionError("ConnectTimeout to huggingface.co")
        )
        assert "hf-mirror.com" in message
        assert "ACV_ASR_HF_ENDPOINT" in message

    def test_other_failure_message_is_truncated(self) -> None:
        adapter = FasterWhisperAdapter()
        message = adapter._describe_load_failure(RuntimeError("x" * 900))
        assert len(message) <= 200


@pytest.mark.unit
class TestWhisperXAdapter:
    def test_engine_name(self) -> None:
        adapter = WhisperXAdapter()
        assert adapter.engine_name == ASREngine.WHISPERX

    async def test_transcribe_not_implemented(self) -> None:
        adapter = WhisperXAdapter()
        with pytest.raises(NotImplementedError):
            await adapter.transcribe("test.wav")


@pytest.mark.unit
class TestQwenASRAdapter:
    def test_engine_name(self) -> None:
        adapter = QwenASRAdapter(api_key="test")
        assert adapter.engine_name == ASREngine.QWEN_ASR

    def test_is_available_with_key(self) -> None:
        adapter = QwenASRAdapter(api_key="test-key")
        assert adapter.is_available() is True

    def test_is_available_without_key(self) -> None:
        adapter = QwenASRAdapter()
        assert adapter.is_available() is False

    async def test_transcribe_not_implemented(self) -> None:
        adapter = QwenASRAdapter(api_key="test")
        with pytest.raises(NotImplementedError):
            await adapter.transcribe("test.wav")


@pytest.mark.unit
class TestASRService:
    def _make_probe_with_audio(self) -> MediaProbe:
        return MediaProbe(
            file_path="test.mp4",
            duration_us=10_000_000,
            width=1920,
            height=1080,
            tracks=[
                MediaTrack(index=0, codec_type="video", codec_name="h264"),
                MediaTrack(index=1, codec_type="audio", codec_name="aac", channels=2, sample_rate=44100),
            ],
        )

    def _make_probe_without_audio(self) -> MediaProbe:
        return MediaProbe(
            file_path="test.mp4",
            duration_us=10_000_000,
            width=1920,
            height=1080,
            tracks=[
                MediaTrack(index=0, codec_type="video", codec_name="h264"),
            ],
        )

    async def test_no_audio_rejected(self) -> None:
        runner = MagicMock(spec=FFmpegRunner)
        provider = MagicMock(spec=ASRProviderProtocol)
        service = ASRService(runner, provider)
        probe = self._make_probe_without_audio()
        with pytest.raises(ASRServiceError, match="无音轨"):
            await service.run_asr(probe)

    async def test_track_index_out_of_range(self) -> None:
        runner = MagicMock()
        runner.get_audio_tracks.return_value = [MediaTrack(index=1, codec_type="audio", codec_name="aac")]
        provider = MagicMock(spec=ASRProviderProtocol)
        service = ASRService(runner, provider)
        probe = self._make_probe_with_audio()
        with pytest.raises(ASRServiceError, match="超出范围"):
            await service.run_asr(probe, audio_track_index=5)

    async def test_empty_result_raises(self) -> None:
        runner = MagicMock()
        runner.get_audio_tracks.return_value = [MediaTrack(index=1, codec_type="audio", codec_name="aac")]
        runner.extract_audio = AsyncMock(return_value="/tmp/output.wav")

        provider = MagicMock(spec=ASRProviderProtocol)
        provider.transcribe = AsyncMock(
            return_value=ASRResult(
                engine=ASREngine.FASTER_WHISPER,
                language="zh",
                model_size=ModelSize.MEDIUM,
                utterances=[],
            )
        )

        service = ASRService(runner, provider)
        probe = self._make_probe_with_audio()
        with pytest.raises(ASRServiceError, match="未检测到语音"):
            await service.run_asr(probe)

    def test_registry_reports_only_installable_engines(self) -> None:
        """可用引擎列表只能包含真能跑的引擎。

        WhisperX / Qwen 的 transcribe 仍会抛 NotImplementedError，把它们列为可用
        会让 UI 显示"识别成功"却没有任何结果。
        """
        from app.providers.registry import get_providers

        engines = get_providers().available_asr_engines()
        assert ASREngine.WHISPERX not in engines
        assert ASREngine.QWEN_ASR not in engines
        assert set(engines) <= {ASREngine.FASTER_WHISPER}

    async def test_unimplemented_adapters_raise_instead_of_faking_success(self) -> None:
        from app.providers.asr.qwen_asr_adapter import QwenASRAdapter
        from app.providers.asr.whisperx_adapter import WhisperXAdapter

        for adapter in (WhisperXAdapter(), QwenASRAdapter(api_key="k")):
            with pytest.raises(NotImplementedError):
                await adapter.transcribe("in.wav")
