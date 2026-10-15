"""Avatar Service — 数字人合成编排。

与主剪辑链路解耦：数字人服务不可用时主剪辑功能不受影响。
"""

from __future__ import annotations

from app.providers.avatar.protocol import (
    AvatarProviderProtocol,
    ProfileResult,
    SynthesisJob,
    VoiceConfig,
)
from app.utils.logging import get_logger

logger = get_logger("services.avatar_service")


class AvatarServiceUnavailableError(RuntimeError):
    """数字人服务不可用（未实现或未配置凭据）。"""


class AvatarService:
    def __init__(self, provider: AvatarProviderProtocol) -> None:
        self._provider = provider

    async def synthesize(
        self,
        text: str,
        avatar_id: str,
        voice_config: VoiceConfig | None = None,
    ) -> SynthesisJob:
        if not self._provider.is_available():
            raise AvatarServiceUnavailableError("数字人服务当前不可用")

        # 不再用 job_id="failed" 兜住异常：那个哨兵值会被当成真实任务号回查
        job = await self._provider.synthesize(text, avatar_id, voice_config)
        logger.info("avatar_synthesize_started", job_id=job.job_id)
        return job

    async def get_job_status(self, job_id: str) -> SynthesisJob:
        return await self._provider.get_job(job_id)

    async def cancel(self, job_id: str) -> bool:
        return await self._provider.cancel_job(job_id)

    async def create_profile(
        self, reference_video_path: str, provider_id: str
    ) -> ProfileResult:
        return await self._provider.create_profile(reference_video_path, provider_id)
