"""ASR Service — 编排 ASR 识别流程。

流程：提取目标音轨为 WAV(16kHz/mono) → 调用 ASR Provider → 存储识别结果至 SQLite
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.config import get_settings
from app.models.asr_result import ASRResult
from app.models.enums import ModelSize
from app.models.media import MediaProbe
from app.providers.asr.protocol import ASRProviderProtocol
from app.providers.ffmpeg.runner import FFmpegRunner
from app.utils.logging import get_logger

logger = get_logger("services.asr_service")


class ASRServiceError(RuntimeError):
    pass


class ASRService:
    def __init__(
        self,
        ffmpeg_runner: FFmpegRunner,
        asr_provider: ASRProviderProtocol,
    ) -> None:
        self.ffmpeg_runner = ffmpeg_runner
        self.asr_provider = asr_provider

    async def run_asr(
        self,
        media_probe: MediaProbe,
        audio_track_index: int = 0,
        language: str = "zh",
        model_size: ModelSize = ModelSize.MEDIUM,
        work_dir: str | Path | None = None,
        pre_extracted_wav: str | Path | None = None,
    ) -> ASRResult:
        """识别音轨。

        `pre_extracted_wav` 让调用方复用已经抽好的波形（16kHz 单声道抽取一遍要几十
        秒，链路里 ASR 与静音检测共用同一份），此时该文件归调用方清理。
        """
        if not media_probe.has_audio:
            raise ASRServiceError("视频无音轨，无法执行 ASR")

        audio_tracks = self.ffmpeg_runner.get_audio_tracks(media_probe)
        if not audio_tracks:
            raise ASRServiceError("未找到可用音轨")
        if audio_track_index >= len(audio_tracks):
            raise ASRServiceError(
                f"音轨索引 {audio_track_index} 超出范围（共 {len(audio_tracks)} 条音轨）"
            )

        owns_wav = pre_extracted_wav is None
        if owns_wav:
            base_dir = Path(work_dir) if work_dir else get_settings().data_dir / "asr_tmp"
            base_dir.mkdir(parents=True, exist_ok=True)
            wav_path = base_dir / f"asr_input_{uuid4().hex[:8]}.wav"
        else:
            wav_path = Path(str(pre_extracted_wav))

        try:
            if owns_wav:
                await self.ffmpeg_runner.extract_audio(
                    media_probe.file_path,
                    audio_track_index,
                    str(wav_path),
                )
                logger.info("audio_extracted_for_asr", file_name=wav_path.name)

            result = await self.asr_provider.transcribe(
                audio_path=str(wav_path),
                language=language,
                model_size=model_size,
            )
        finally:
            # 16kHz 单声道 WAV 每小时约 115 MB，泄漏会迅速吃满磁盘
            if owns_wav:
                try:
                    wav_path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("asr_temp_cleanup_failed", file_name=wav_path.name)

        if not result.utterances:
            logger.warning("asr_no_speech_detected", file=media_probe.file_path)
            raise ASRServiceError("未检测到语音内容")

        logger.info(
            "asr_completed",
            utterances=len(result.utterances),
            engine=result.engine.value,
        )
        return result
